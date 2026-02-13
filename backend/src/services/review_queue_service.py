"""Review queue service with field-level locking, atomic claims, and SLA tracking.

All database operations use PostgreSQL via asyncpg with explicit transactions
for atomicity.  Manually-corrected fields are *locked* and never overwritten
by re-extraction.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from src.config import settings
from src.models.schemas import (
    ExtractedField,
    ExtractionResult,
    QueueStats,
    ReviewAction,
    ReviewItem,
    ReviewStatus,
    ReviewSubmission,
)
from src.services.database import get_db, release_db

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# Priority calculation


def calculate_priority(
    confidence_avg: float,
    sla_deadline: datetime | None,
    num_line_items: int = 0,
    total_amount: float = 0.0,
) -> float:
    """Compute review priority (higher = more urgent).

    Formula
    priority = (100 - confidence_avg*100) * 0.4
             + (hours_until_sla / 24)      * 0.3
             + (num_line_items / 100)       * 0.2
             + (total_amount / 10_000)      * 0.1
    """
    conf_score = (100 - confidence_avg * 100) * 0.4

    if sla_deadline:
        now = datetime.now(timezone.utc)
        if sla_deadline.tzinfo is None:
            sla_deadline = sla_deadline.replace(tzinfo=timezone.utc)
        hours_left = max((sla_deadline - now).total_seconds() / 3600, 0)
        sla_score = max(0, (24 - hours_left) / 24) * 0.3 * 100  # closer → higher
    else:
        sla_score = 0.0

    items_score = min(num_line_items / 100, 1.0) * 0.2 * 100
    value_score = min(total_amount / 10_000, 1.0) * 0.1 * 100

    return round(conf_score + sla_score + items_score + value_score, 2)


# Service


class ReviewQueueService:
    """Async service managing the human-review queue."""

    # Create

    async def create_item(self, result: ExtractionResult) -> ReviewItem:
        """Insert a new review item + extracted fields from an ExtractionResult."""
        item_id = _uuid()
        now = _utcnow()
        # SLA deadline is NOT set at creation — it starts when
        # the reviewer clicks "Start Review" (claim_item).
        sla_deadline = None

        priority = calculate_priority(
            confidence_avg=result.overall_confidence,
            sla_deadline=sla_deadline,
            num_line_items=len(result.invoice_data.line_items),
            total_amount=result.invoice_data.total or 0,
        )

        db = await get_db()
        try:
            async with db.transaction():
                await db.execute(
                    """INSERT INTO review_items
                       (id, document_id, filename, status, priority, sla_deadline, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    item_id,
                    result.document_id,
                    result.filename,
                    ReviewStatus.PENDING.value,
                    priority,
                    sla_deadline,
                    now,
                )

                # Insert extracted fields
                for fc in result.field_confidences:
                    field_id = _uuid()
                    value_str = (
                        fc.value
                        if isinstance(fc.value, str)
                        else str(fc.value)
                        if fc.value is not None
                        else None
                    )
                    await db.execute(
                        """INSERT INTO extracted_fields
                           (id, review_item_id, field_name, value, confidence)
                           VALUES ($1, $2, $3, $4, $5)""",
                        field_id,
                        item_id,
                        fc.field_name,
                        value_str,
                        fc.confidence,
                    )
        finally:
            await release_db(db)

        logger.info(
            "Created review item %s for doc %s (priority=%.1f)",
            item_id,
            result.document_id,
            priority,
        )
        return await self.get_item(item_id)

    # Read

    async def get_item(self, item_id: str) -> ReviewItem | None:
        """Fetch a single review item with its extracted fields."""
        db = await get_db()
        try:
            row = await db.fetchrow("SELECT * FROM review_items WHERE id = $1", item_id)
            if row is None:
                return None

            item = self._row_to_item(row)

            fields = await db.fetch(
                "SELECT * FROM extracted_fields WHERE review_item_id = $1",
                item_id,
            )
            item.fields = [self._row_to_field(f) for f in fields]
            return item
        finally:
            await release_db(db)

    async def get_queue(
        self,
        status: str | None = None,
        assigned_to: str | None = None,
        priority_min: float | None = None,
        sort_by: str = "priority",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReviewItem], int]:
        """Return paginated queue items, ordered by priority (descending)."""
        conditions: list[str] = []
        params: list = []
        idx = 1  # asyncpg uses $1, $2, ...

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if assigned_to:
            conditions.append(f"assigned_to = ${idx}")
            params.append(assigned_to)
            idx += 1
        if priority_min is not None:
            conditions.append(f"priority >= ${idx}")
            params.append(priority_min)
            idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        allowed_sorts = {
            "priority": "priority DESC",
            "sla": "sla_deadline ASC",
            "date": "created_at DESC",
        }
        order = allowed_sorts.get(sort_by, "priority DESC")

        db = await get_db()
        try:
            # Total count
            count_row = await db.fetchrow(
                f"SELECT COUNT(*) AS cnt FROM review_items {where}", *params
            )
            total = count_row["cnt"]

            # Page
            rows = await db.fetch(
                f"SELECT * FROM review_items {where} ORDER BY {order} LIMIT ${idx} OFFSET ${idx + 1}",
                *params,
                limit,
                offset,
            )

            items: list[ReviewItem] = []
            item_ids: list[str] = []
            for row in rows:
                item = self._row_to_item(row)
                items.append(item)
                item_ids.append(item.id)

            # Batch-fetch all fields for these items (fixes N+1 query)
            if item_ids:
                all_fields = await db.fetch(
                    "SELECT * FROM extracted_fields WHERE review_item_id = ANY($1::text[])",
                    item_ids,
                )
                # Group fields by review_item_id
                fields_by_item: dict[str, list[ExtractedField]] = {}
                for f_row in all_fields:
                    rid = f_row["review_item_id"]
                    if rid not in fields_by_item:
                        fields_by_item[rid] = []
                    fields_by_item[rid].append(self._row_to_field(f_row))

                for item in items:
                    item.fields = fields_by_item.get(item.id, [])

            return items, total
        finally:
            await release_db(db)

    # Claim (atomic)

    async def claim_item(self, item_id: str, reviewer_id: str) -> ReviewItem | None:
        """Start review: transition a pending item to in_review.

        Sets claimed_at and sla_deadline (SLA starts NOW, not at creation).
        Only works on items in 'pending' status.
        """
        now = _utcnow()
        sla_deadline = now + timedelta(hours=settings.sla_default_hours)
        db = await get_db()
        try:
            async with db.transaction():
                result = await db.execute(
                    """UPDATE review_items
                       SET status = $1, assigned_to = $2, claimed_at = $3,
                           sla_deadline = $4
                       WHERE id = $5 AND status = $6""",
                    ReviewStatus.IN_REVIEW.value,
                    reviewer_id,
                    now,
                    sla_deadline,
                    item_id,
                    ReviewStatus.PENDING.value,
                )
                # asyncpg returns "UPDATE N" string
                rows_affected = int(result.split()[-1])
                if rows_affected == 0:
                    return None  # already in_review or completed

                await self._audit(db, item_id, "start_review", actor=reviewer_id)
        finally:
            await release_db(db)

        return await self.get_item(item_id)

    # Submit review

    async def submit_review(
        self,
        item_id: str,
        submission: ReviewSubmission,
        reviewer_id: str = "system",
    ) -> ReviewItem | None:
        """Submit a review decision (approve / correct / reject).

        Corrections update field values but **never overwrite locked fields**.
        """
        action_to_status = {
            ReviewAction.APPROVE: ReviewStatus.APPROVED,
            ReviewAction.CORRECT: ReviewStatus.CORRECTED,
            ReviewAction.REJECT: ReviewStatus.REJECTED,
        }
        new_status = action_to_status[submission.action]
        now = _utcnow()

        db = await get_db()
        try:
            async with db.transaction():
                # Update item status
                await db.execute(
                    """UPDATE review_items
                       SET status = $1, completed_at = $2
                       WHERE id = $3""",
                    new_status.value,
                    now,
                    item_id,
                )

                # Apply corrections (skip locked fields!)
                if submission.corrections:
                    for field_name, new_value in submission.corrections.items():
                        # Check lock
                        row = await db.fetchrow(
                            "SELECT id, value, locked FROM extracted_fields WHERE review_item_id = $1 AND field_name = $2",
                            item_id,
                            field_name,
                        )
                        if row is None:
                            continue
                        if row["locked"]:
                            logger.info(
                                "Skipping locked field %s on %s", field_name, item_id
                            )
                            continue

                        old_value = row["value"]
                        await db.execute(
                            """UPDATE extracted_fields
                               SET value = $1, manually_corrected = TRUE, corrected_at = $2,
                                   corrected_by = $3, locked = TRUE
                               WHERE id = $4""",
                            new_value,
                            now,
                            reviewer_id,
                            row["id"],
                        )
                        await self._audit(
                            db,
                            item_id,
                            "correction",
                            field_name=field_name,
                            old_value=old_value,
                            new_value=new_value,
                            actor=reviewer_id,
                        )

                # Rejection reason → audit log
                if submission.action == ReviewAction.REJECT and submission.reason:
                    await self._audit(
                        db,
                        item_id,
                        "rejection",
                        new_value=submission.reason,
                        actor=reviewer_id,
                    )

                # Approval → audit log
                if submission.action == ReviewAction.APPROVE:
                    await self._audit(
                        db,
                        item_id,
                        "approval",
                        actor=reviewer_id,
                    )
        finally:
            await release_db(db)

        return await self.get_item(item_id)

    # Stats

    async def get_stats(self) -> QueueStats:
        """Compute dashboard statistics."""
        db = await get_db()
        try:
            # Queue depth (pending + in_review)
            row = await db.fetchrow(
                "SELECT COUNT(*) AS cnt FROM review_items WHERE status IN ('pending', 'in_review')"
            )
            depth = row["cnt"]

            # Items reviewed today
            today = datetime.now(timezone.utc).date()
            row = await db.fetchrow(
                "SELECT COUNT(*) AS cnt FROM review_items WHERE completed_at IS NOT NULL AND completed_at >= $1",
                datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
            )
            reviewed_today = row["cnt"]

            # Avg review time (PostgreSQL epoch extraction)
            row = await db.fetchrow(
                """SELECT AVG(
                     EXTRACT(EPOCH FROM (completed_at::timestamptz - claimed_at::timestamptz))
                   ) AS avg_time FROM review_items
                   WHERE completed_at IS NOT NULL AND claimed_at IS NOT NULL"""
            )
            avg_time = row["avg_time"] or 0.0

            # SLA compliance
            row = await db.fetchrow(
                "SELECT COUNT(*) AS cnt FROM review_items WHERE completed_at IS NOT NULL"
            )
            total_completed = row["cnt"]

            row = await db.fetchrow(
                """SELECT COUNT(*) AS cnt FROM review_items
                   WHERE completed_at IS NOT NULL
                     AND completed_at <= sla_deadline"""
            )
            on_time = row["cnt"]

            sla_pct = (
                (on_time / total_completed * 100) if total_completed > 0 else 100.0
            )

            return QueueStats(
                queue_depth=depth,
                items_reviewed_today=reviewed_today,
                avg_review_time_seconds=round(avg_time, 1),
                sla_compliance_percent=round(sla_pct, 1),
            )
        finally:
            await release_db(db)

    # Helpers

    @staticmethod
    def _row_to_item(row: asyncpg.Record) -> ReviewItem:
        return ReviewItem(
            id=row["id"],
            document_id=row["document_id"],
            filename=row["filename"],
            status=ReviewStatus(row["status"]),
            priority=row["priority"] or 0,
            sla_deadline=row["sla_deadline"].isoformat()
            if row["sla_deadline"]
            else None,
            assigned_to=row["assigned_to"],
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
            claimed_at=row["claimed_at"].isoformat() if row["claimed_at"] else None,
            completed_at=row["completed_at"].isoformat()
            if row["completed_at"]
            else None,
        )

    @staticmethod
    def _row_to_field(row: asyncpg.Record) -> ExtractedField:
        return ExtractedField(
            id=row["id"],
            review_item_id=row["review_item_id"],
            field_name=row["field_name"],
            value=row["value"],
            confidence=row["confidence"] or 0,
            manually_corrected=bool(row["manually_corrected"]),
            corrected_at=row["corrected_at"].isoformat()
            if row["corrected_at"]
            else None,
            corrected_by=row["corrected_by"],
            locked=bool(row["locked"]),
        )

    @staticmethod
    async def _audit(
        db: asyncpg.Connection,
        item_id: str,
        action: str,
        field_name: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        actor: str | None = None,
    ) -> None:
        await db.execute(
            """INSERT INTO audit_log (item_id, action, field_name, old_value, new_value, actor, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            item_id,
            action,
            field_name,
            old_value,
            new_value,
            actor,
            _utcnow(),
        )

    # Claim expiry

    async def release_expired_claims(self) -> int:
        """Release items stuck in 'in_review' past the expiry window.

        Returns the number of items released back to 'pending'.
        """
        expiry_minutes = settings.claim_expiry_minutes
        cutoff = _utcnow() - timedelta(minutes=expiry_minutes)

        db = await get_db()
        try:
            async with db.transaction():
                result = await db.execute(
                    """UPDATE review_items
                       SET status = 'pending', assigned_to = NULL,
                           claimed_at = NULL, sla_deadline = NULL
                       WHERE status = 'in_review' AND claimed_at < $1""",
                    cutoff,
                )
                released = int(result.split()[-1])

                if released > 0:
                    logger.info(
                        "Released %d expired claims (older than %d min)",
                        released,
                        expiry_minutes,
                    )
        finally:
            await release_db(db)

        return released

    # Least-loaded auto-assign

    async def auto_assign(self, item_id: str) -> ReviewItem | None:
        """Assign a pending item to the least-loaded reviewer.

        Only sets ``assigned_to`` — status stays 'pending'.
        The reviewer must click 'Start Review' to transition to 'in_review'
        (which is when the SLA countdown begins).
        """
        roster = settings.reviewer_roster
        if not roster:
            return None

        # Determine least-loaded reviewer
        workload = await self.get_reviewer_workload()
        candidates = [(workload.get(r, 0), r) for r in roster]
        min_load = min(c[0] for c in candidates)
        tied = [r for load, r in candidates if load == min_load]
        reviewer = tied[0]  # simple first-match tie-break for async path

        now = _utcnow()
        db = await get_db()
        try:
            async with db.transaction():
                result = await db.execute(
                    """UPDATE review_items
                       SET assigned_to = $1
                       WHERE id = $2 AND status = $3""",
                    reviewer,
                    item_id,
                    ReviewStatus.PENDING.value,
                )
                rows_affected = int(result.split()[-1])
                if rows_affected == 0:
                    return None  # already claimed

                await self._audit(db, item_id, "auto_assign", actor=reviewer)
        finally:
            await release_db(db)

        return await self.get_item(item_id)

    # Audit trail retrieval

    async def get_audit_trail(self, item_id: str) -> list[dict]:
        """Return the full audit trail for a review item, newest first."""
        db = await get_db()
        try:
            rows = await db.fetch(
                """SELECT id, item_id, action, field_name, old_value, new_value, actor, created_at
                   FROM audit_log WHERE item_id = $1 ORDER BY created_at DESC""",
                item_id,
            )
            return [
                {
                    "id": r["id"],
                    "item_id": r["item_id"],
                    "action": r["action"],
                    "field_name": r["field_name"],
                    "old_value": r["old_value"],
                    "new_value": r["new_value"],
                    "actor": r["actor"],
                    "created_at": r["created_at"].isoformat()
                    if r["created_at"]
                    else None,
                }
                for r in rows
            ]
        finally:
            await release_db(db)

    # Load-balanced assignment

    async def get_reviewer_workload(self) -> dict[str, int]:
        """Return a mapping of reviewer_id → number of assigned items (pending + in_review)."""
        db = await get_db()
        try:
            rows = await db.fetch(
                """SELECT assigned_to, COUNT(*) AS cnt
                   FROM review_items
                   WHERE status IN ('pending', 'in_review')
                     AND assigned_to IS NOT NULL
                   GROUP BY assigned_to"""
            )
            return {r["assigned_to"]: r["cnt"] for r in rows}
        finally:
            await release_db(db)

    async def suggest_reviewer(
        self, known_reviewers: list[str] | None = None
    ) -> str | None:
        """Suggest the least-loaded reviewer from the known reviewer list.

        If no reviewer list is provided, returns the reviewer with the fewest
        active items, or None if no reviewers are active.
        """
        workload = await self.get_reviewer_workload()

        if known_reviewers:
            # Pick the reviewer with the fewest items (round-robin tie-break)
            best = min(known_reviewers, key=lambda r: workload.get(r, 0))
            return best

        if not workload:
            return None

        return min(workload, key=workload.get)  # type: ignore[arg-type]
