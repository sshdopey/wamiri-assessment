"""FastAPI REST endpoints for the document processing dashboard.

Endpoints
─────────
POST   /api/documents/upload        – Upload PDF/image & trigger extraction
GET    /api/documents               – List tracked documents (with status)
GET    /api/documents/{doc_id}      – Single document status
GET    /api/documents/{doc_id}/preview – Preview uploaded file (PDF or image)
GET    /api/documents/{doc_id}/download/{fmt} – Download result file
GET    /api/queue                    – List review queue (paginated)
GET    /api/queue/{item_id}          – Single review item detail
POST   /api/queue/{item_id}/claim    – Atomically claim item for review
PUT    /api/queue/{item_id}/submit   – Submit review decision
GET    /api/stats                    – Dashboard statistics
GET    /api/metrics                  – Prometheus metrics
"""

from __future__ import annotations

import shutil
import uuid
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from src.config import settings
from src.models.schemas import (
    ClaimRequest,
    Document,
    DocumentListResponse,
    PaginatedResponse,
    QueueStats,
    ReviewItem,
    ReviewSubmission,
    UploadResponse,
)
from src.services.review_queue_service import ReviewQueueService
from src.services.extraction_service import SUPPORTED_MIME_TYPES

logger = logging.getLogger(__name__)
router = APIRouter()

queue_service = ReviewQueueService()

# Allowed upload extensions
_ALLOWED_EXTENSIONS = set(SUPPORTED_MIME_TYPES.keys())


def _get_file_extension(filename: str) -> str | None:
    """Extract and validate file extension."""
    if "." not in filename:
        return None
    ext = "." + filename.rsplit(".", 1)[-1].lower()
    return ext if ext in _ALLOWED_EXTENSIONS else None

# ── Upload ────────────────────────────────────────────────────────────────────


@router.post("/documents/upload", response_model=UploadResponse)
async def upload_document(file: UploadFile = File(...)):
    """Accept a PDF or image, persist it, track in DB, and trigger Celery extraction."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = _get_file_extension(file.filename)
    if ext is None:
        allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {allowed}",
        )

    mime_type = SUPPORTED_MIME_TYPES[ext]
    doc_id = str(uuid.uuid4())
    stored_filename = f"{doc_id}{ext}"

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / stored_filename

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Insert document record IMMEDIATELY (before Celery)
    from src.services.database import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO documents (id, filename, original_filename, mime_type, status, created_at, updated_at)
               VALUES ($1, $2, $3, $4, 'queued', NOW(), NOW())""",
            doc_id,
            stored_filename,
            file.filename,
            mime_type,
        )

    # Trigger Celery task
    task_id = "sync-fallback"
    try:
        from src.tasks.celery_app import process_document_task

        task = process_document_task.delay(doc_id, str(dest), stored_filename)
        task_id = task.id

        # Update document with task_id
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET task_id = $1, updated_at = NOW() WHERE id = $2",
                task_id,
                doc_id,
            )
    except Exception as exc:
        logger.warning("Celery unavailable, skipping async task: %s", exc)

    return UploadResponse(
        document_id=doc_id,
        task_id=task_id,
        filename=file.filename,
        mime_type=mime_type,
    )


# ── Document listing & status ─────────────────────────────────────────────────


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all tracked documents with their processing status."""
    from src.services.database import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        where_clause = ""
        params: list = []
        if status:
            where_clause = "WHERE status = $1"
            params.append(status)

        count_sql = f"SELECT COUNT(*) FROM documents {where_clause}"
        total = await conn.fetchval(count_sql, *params)

        # Add limit/offset params
        param_offset = len(params) + 1
        query_sql = f"""
            SELECT id, filename, original_filename, mime_type, status,
                   task_id, error_message, created_at, updated_at
            FROM documents {where_clause}
            ORDER BY created_at DESC
            LIMIT ${param_offset} OFFSET ${param_offset + 1}
        """
        params.extend([limit, offset])
        rows = await conn.fetch(query_sql, *params)

    items = [
        Document(
            id=r["id"],
            filename=r["filename"],
            original_filename=r["original_filename"],
            mime_type=r["mime_type"],
            status=r["status"],
            task_id=r["task_id"],
            error_message=r["error_message"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return DocumentListResponse(items=items, total=total)


@router.get("/documents/{doc_id}/status")
async def get_document_status(doc_id: str):
    """Get the current processing status of a document."""
    from src.services.database import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, filename, original_filename, mime_type, status, task_id, error_message, created_at, updated_at FROM documents WHERE id = $1",
            doc_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    return Document(
        id=row["id"],
        filename=row["filename"],
        original_filename=row["original_filename"],
        mime_type=row["mime_type"],
        status=row["status"],
        task_id=row["task_id"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Queue listing ─────────────────────────────────────────────────────────────


@router.get("/queue", response_model=PaginatedResponse)
async def get_queue(
    status: Optional[str] = Query(None, description="Filter by status"),
    priority_min: Optional[float] = Query(None),
    sort_by: str = Query("priority", pattern="^(priority|sla|date)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List review-queue items with filtering, sorting, and pagination."""
    items, total = await queue_service.get_queue(
        status=status,
        priority_min=priority_min,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


# ── Single item ───────────────────────────────────────────────────────────────


@router.get("/queue/{item_id}", response_model=ReviewItem)
async def get_queue_item(item_id: str):
    """Get full detail for a single review item including extracted fields."""
    item = await queue_service.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


# ── Claim ─────────────────────────────────────────────────────────────────────


@router.post("/queue/{item_id}/claim", response_model=ReviewItem)
async def claim_item(item_id: str, body: ClaimRequest):
    """Atomically claim a pending item for review."""
    item = await queue_service.claim_item(item_id, body.reviewer_id)
    if item is None:
        raise HTTPException(
            status_code=409,
            detail="Item already claimed or not found",
        )
    return item


# ── Submit review ─────────────────────────────────────────────────────────────


@router.put("/queue/{item_id}/submit", response_model=ReviewItem)
async def submit_review(item_id: str, body: ReviewSubmission):
    """Submit a review decision (approve / correct / reject)."""
    item = await queue_service.submit_review(
        item_id=item_id,
        submission=body,
        reviewer_id="reviewer-1",  # In production: from auth token
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


# ── Stats ─────────────────────────────────────────────────────────────────────


@router.get("/stats", response_model=QueueStats)
async def get_stats():
    """Return dashboard statistics."""
    return await queue_service.get_stats()


# ── File download ─────────────────────────────────────────────────────────────


@router.get("/documents/{doc_id}/download/{fmt}")
async def download_result(doc_id: str, fmt: str):
    """Download extraction result as Parquet or JSON."""
    if fmt not in ("parquet", "json"):
        raise HTTPException(status_code=400, detail="Format must be 'parquet' or 'json'")

    base = settings.parquet_dir if fmt == "parquet" else settings.json_dir

    # Search date-partitioned dirs
    for path in sorted(base.rglob(f"{doc_id}.{fmt}"), reverse=True):
        return FileResponse(
            path=str(path),
            media_type="application/octet-stream" if fmt == "parquet" else "application/json",
            filename=f"{doc_id}.{fmt}",
        )

    raise HTTPException(status_code=404, detail="File not found")


# ── Document preview ──────────────────────────────────────────────────────────


@router.get("/documents/{doc_id}/preview")
async def preview_document(doc_id: str):
    """Return the uploaded document (PDF or image) for preview in the dashboard."""
    upload_dir = Path(settings.upload_dir)

    # Look up the stored filename + build a display name from extracted data
    from src.services.database import get_pool

    pool = await get_pool()
    display_name: str | None = None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT filename, original_filename, mime_type FROM documents WHERE id = $1",
            doc_id,
        )

        # Try to build "Vendor — Invoice#.ext" from extracted fields
        ri_row = await conn.fetchrow(
            "SELECT id FROM review_items WHERE document_id = $1", doc_id,
        )
        if ri_row:
            fields = await conn.fetch(
                "SELECT field_name, value FROM extracted_fields WHERE review_item_id = $1",
                ri_row["id"],
            )
            field_map = {f["field_name"]: f["value"] for f in fields}
            vendor = field_map.get("vendor")
            inv_num = field_map.get("invoice_number")
            if vendor and inv_num:
                display_name = f"{vendor} — {inv_num}"
            elif vendor:
                display_name = vendor
            elif inv_num:
                display_name = f"Invoice {inv_num}"

    if row:
        file_path = upload_dir / row["filename"]
        mime_type = row["mime_type"]
        # Determine the file extension from the stored filename
        ext = Path(row["filename"]).suffix
        if display_name:
            filename = f"{display_name}{ext}"
        else:
            filename = row["original_filename"]

        if file_path.exists():
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                filename=filename,
                content_disposition_type="inline",
            )

    # Fallback: scan upload dir for any file starting with doc_id
    for ext in SUPPORTED_MIME_TYPES:
        candidate = upload_dir / f"{doc_id}{ext}"
        if candidate.exists():
            return FileResponse(
                path=str(candidate),
                media_type=SUPPORTED_MIME_TYPES[ext],
                content_disposition_type="inline",
            )

    # Also check documents dir
    docs_dir = Path(settings.documents_dir)
    if docs_dir.exists():
        for p in docs_dir.iterdir():
            if doc_id in p.stem and p.suffix.lower() in SUPPORTED_MIME_TYPES:
                return FileResponse(
                    path=str(p),
                    media_type=SUPPORTED_MIME_TYPES[p.suffix.lower()],
                    content_disposition_type="inline",
                )

    raise HTTPException(status_code=404, detail="Document not found")


# ── Prometheus metrics ────────────────────────────────────────────────────────


@router.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
