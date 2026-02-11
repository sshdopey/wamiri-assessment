"""Data quality tests – schema compliance, Parquet/JSON consistency."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.models.schemas import ExtractionResult


class TestDualFormatConsistency:
    """Parquet and JSON outputs must contain identical data."""

    def test_dual_output_contains_same_data(
        self, tmp_dir: Path, sample_extraction_result
    ):
        """Write both formats and compare key fields."""
        from src.services.storage_service import StorageService

        svc = StorageService()
        parquet_path, json_path = svc.save_result(sample_extraction_result)

        # Read JSON
        with open(json_path) as f:
            json_data = json.load(f)

        # Read Parquet
        table = pq.read_table(str(parquet_path))
        df = table.to_pandas()

        assert len(df) == 1
        row = df.iloc[0]

        assert row["document_id"] == json_data["document_id"]
        assert row["vendor"] == json_data["invoice_data"]["vendor"]
        assert row["invoice_number"] == json_data["invoice_data"]["invoice_number"]
        assert float(row["total"]) == json_data["invoice_data"]["total"]
        assert row["currency"] == json_data["invoice_data"]["currency"]

    def test_parquet_schema_correct(self, tmp_dir: Path, sample_extraction_result):
        """Parquet file has the expected column names."""
        from src.services.storage_service import StorageService

        svc = StorageService()
        parquet_path, _ = svc.save_result(sample_extraction_result)

        table = pq.read_table(str(parquet_path))
        names = table.schema.names

        expected = [
            "document_id", "filename", "vendor", "invoice_number",
            "date", "due_date", "subtotal", "tax_rate", "tax_amount",
            "total", "currency", "num_line_items", "line_items_json",
            "confidence_score", "extracted_at", "content_hash",
        ]
        for col in expected:
            assert col in names, f"Missing column: {col}"

    def test_json_no_null_required_fields(
        self, tmp_dir: Path, sample_extraction_result
    ):
        """Required fields in JSON output must not be null."""
        from src.services.storage_service import StorageService

        svc = StorageService()
        _, json_path = svc.save_result(sample_extraction_result)

        with open(json_path) as f:
            data = json.load(f)

        assert data["document_id"] is not None
        assert data["filename"] is not None
        assert data["invoice_data"]["vendor"] is not None
        assert data["invoice_data"]["total"] is not None

    def test_atomic_write_leaves_no_temp_files(
        self, tmp_dir: Path, sample_extraction_result
    ):
        """After successful write, no .tmp files remain."""
        from src.services.storage_service import StorageService

        svc = StorageService()
        svc.save_result(sample_extraction_result)

        # Check data dirs for stale tmp files
        for p in Path("./data").rglob("*.tmp"):
            pytest.fail(f"Leftover temp file: {p}")
