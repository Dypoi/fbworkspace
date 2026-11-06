"""Uji ekspor CSV, terutama perlindungan formula injection."""

from __future__ import annotations

import csv
import io

from fbworkspace.export import (
    EXPORT_COLUMNS,
    rows_to_csv,
    rows_to_csv_bytes,
    sanitize_cell,
    suggested_filename,
)


class TestSanitizeCell:
    def test_formula_prefixes_are_neutralized(self):
        for payload in ("=SUM(A1:A2)", "+1+1", "-2+3", "@SUM(1)", "\t=cmd", "\r=1"):
            cleaned = sanitize_cell(payload)
            assert cleaned.startswith("'"), payload

    def test_leading_space_then_formula_is_neutralized(self):
        assert sanitize_cell("  =cmd|' /C calc'!A0").startswith("'")

    def test_plain_text_untouched(self):
        assert sanitize_cell("Dijual iPhone 13 mulus") == "Dijual iPhone 13 mulus"

    def test_none_becomes_empty(self):
        assert sanitize_cell(None) == ""

    def test_null_bytes_removed(self):
        assert "\x00" not in sanitize_cell("iPhone\x00 13")

    def test_long_text_truncated(self):
        assert len(sanitize_cell("x" * 9000)) < 9000

    def test_negative_number_string_is_prefixed(self):
        assert sanitize_cell(-5000) == "'-5000"


class TestCsvOutput:
    def _row(self, **overrides):
        base = {
            "id": 1,
            "group_url": "https://www.facebook.com/groups/1/",
            "post_url": "https://www.facebook.com/groups/1/posts/2/",
            "text": "Dijual iPhone 13 Pro Max Rp 12.500.000",
            "found_at": "2026-01-01T00:00:00+00:00",
            "posted_at": None,
            "posted_at_precision": "tidak diketahui",
            "source": "manual",
            "workflow_status": "baru",
            "category": "menjual",
            "iphone_model": "iPhone 13 Pro Max",
            "price_amount": 12_500_000,
            "price_kind": "harga",
            "location": "Tangerang",
            "details": {"storage": "256 GB", "battery_health": "89%"},
            "relevance_score": 100,
            "reasons": ["+40 menyebut iPhone.", "+15 model teridentifikasi."],
            "snippet": "Dijual iPhone 13 Pro Max",
        }
        base.update(overrides)
        return base

    def test_header_matches_defined_columns(self):
        content = rows_to_csv([])
        rows = list(csv.reader(io.StringIO(content)))
        assert rows[0] == [label for _key, label in EXPORT_COLUMNS]

    def test_row_values_and_details(self):
        content = rows_to_csv([self._row()])
        reader = list(csv.reader(io.StringIO(content)))
        assert len(reader) == 2
        header, row = reader[0], reader[1]
        record = dict(zip(header, row))
        assert record["Model iPhone"] == "iPhone 13 Pro Max"
        assert record["Nominal"] == "12500000"
        assert "storage: 256 GB" in record["Detail Disebutkan"]
        assert "iPhone" in record["Alasan"]

    def test_injection_in_text_is_neutralized(self):
        content = rows_to_csv([self._row(text="=HYPERLINK(\"http://evil\")")])
        reader = list(csv.reader(io.StringIO(content)))
        text_cell = reader[1][3]
        assert text_cell.startswith("'")
        assert "HYPERLINK" in text_cell

    def test_newlines_preserved_inside_quoted_cell(self):
        content = rows_to_csv([self._row(text="baris satu\nbaris dua")])
        reader = list(csv.reader(io.StringIO(content)))
        assert "baris dua" in reader[1][3]

    def test_bytes_have_utf8_bom(self):
        data = rows_to_csv_bytes([self._row()])
        assert data.startswith(b"\xef\xbb\xbf")
        assert "iPhone" in data.decode("utf-8-sig")

    def test_lists_are_stringified_safely(self):
        content = rows_to_csv([self._row(reasons=["=1", "ok"])])
        reader = list(csv.reader(io.StringIO(content)))
        reasons_cell = reader[1][16]
        assert "'=1" in reasons_cell

    def test_filename_pattern(self):
        name = suggested_filename()
        assert name.startswith("fbworkspace_")
        assert name.endswith(".csv")
