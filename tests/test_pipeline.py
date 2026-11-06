"""Uji alur input manual & penyimpanan postingan."""

from __future__ import annotations

from fbworkspace.models import FilterConfig
from fbworkspace.pipeline import (
    ingest_manual_entries,
    ingest_post,
    parse_manual_input,
)


class TestManualParsing:
    def test_single_block(self):
        entries = parse_manual_input("Dijual iPhone 13 murah")
        assert len(entries) == 1
        assert entries[0].text == "Dijual iPhone 13 murah"

    def test_multiple_blocks(self):
        raw = "iPhone 13 8jt\n\n---\n\niPhone 11 4jt\n\n----\n\niPhone 12 6jt"
        entries = parse_manual_input(raw)
        assert [e.text for e in entries] == ["iPhone 13 8jt", "iPhone 11 4jt", "iPhone 12 6jt"]

    def test_headers_are_extracted(self):
        raw = (
            "URL: https://www.facebook.com/groups/1/posts/2/\n"
            "GRUP: https://www.facebook.com/groups/1/\n"
            "WAKTU: 3 jam lalu\n"
            "\n"
            "Dijual iPhone 13 128GB 8jt\n"
        )
        entry = parse_manual_input(raw)[0]
        assert entry.post_url == "https://www.facebook.com/groups/1/posts/2/"
        assert entry.group_url == "https://www.facebook.com/groups/1/"
        assert entry.posted_at_raw == "3 jam lalu"
        assert "Dijual iPhone 13" in entry.text

    def test_header_lines_not_included_in_text(self):
        raw = "URL: https://x/\nDijual iPhone 13"
        assert "URL:" not in parse_manual_input(raw)[0].text

    def test_empty_input(self):
        assert parse_manual_input("") == []
        assert parse_manual_input("\n---\n") == []


class TestIngest:
    def test_store_with_filter_and_analysis(self, db):
        result = ingest_post(
            db,
            text="Dijual iPhone 13 Pro Max 256GB Rp 12.500.000 COD Tangerang",
            source="manual",
            filter_config=FilterConfig(include_keywords=["iphone"]),
        )
        assert result.is_new
        assert result.filter_passed
        post = db.get_post(result.post_id)
        assert post["analysis_status"] == "dianalisis (aturan)"
        assert db.get_analysis(result.post_id)["iphone_model"] == "iPhone 13 Pro Max"

    def test_filtered_out_post_is_still_stored_with_flag(self, db):
        result = ingest_post(
            db,
            text="Jual kulkas murah",
            source="manual",
            filter_config=FilterConfig(require_iphone=True),
        )
        assert result.is_new
        assert not result.filter_passed
        assert db.get_post(result.post_id)["filter_passed"] == 0

    def test_duplicate_returns_existing_id(self, db):
        first = ingest_post(db, text="iPhone 13 8jt", source="manual",
                            filter_config=FilterConfig())
        second = ingest_post(db, text="iPhone 13 8jt", source="manual",
                             filter_config=FilterConfig())
        assert first.post_id == second.post_id
        assert not second.is_new

    def test_empty_text_reports_error(self, db):
        result = ingest_post(db, text="  ", filter_config=FilterConfig())
        assert result.error
        assert db.count_posts() == 0

    def test_posted_at_relative_is_marked_approx(self, db):
        result = ingest_post(
            db,
            text="iPhone 13 8jt",
            posted_at_raw="2 jam lalu",
            filter_config=FilterConfig(),
        )
        post = db.get_post(result.post_id)
        assert post["posted_at"] is not None
        assert post["posted_at_precision"] == "perkiraan"


class TestManualBatch:
    def test_batch_summary(self, db):
        entries = parse_manual_input(
            "Dijual iPhone 13 8jt\n\n---\n\nJual kulkas 1jt\n\n---\n\nDijual iPhone 13 8jt"
        )
        summary = ingest_manual_entries(db, entries, filter_config=FilterConfig())
        assert summary.read == 3
        assert summary.inserted == 2
        assert summary.duplicates == 1
        assert summary.filtered_out == 1

    def test_max_posts_limit(self, db):
        entries = parse_manual_input("\n---\n".join(f"iPhone 1{i} murah" for i in range(10)))
        summary = ingest_manual_entries(
            db, entries, filter_config=FilterConfig(), max_posts=3
        )
        assert summary.read == 3
        assert db.count_posts() == 3
