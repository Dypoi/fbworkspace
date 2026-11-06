"""Uji data pendukung UI 3 langkah.

Mencakup: hitungan per status filter, pratinjau filter, "terapkan ulang filter"
(tanpa menggandakan baris), dan hapus per sumber (DEMO).
"""

from __future__ import annotations

from fbworkspace.filters import evaluate
from fbworkspace.models import FilterConfig
from fbworkspace.pipeline import preview_filter_counts, reapply_filters

IPHONE_TEXT = "Dijual iPhone 13 128GB mulus, harga 8.500.000, COD Tangerang."
OTHER_TEXT = "Dijual kulkas 2 pintu murah, COD Depok."


def _insert(db, text: str, *, passed: bool, source: str = "manual") -> int:
    post_id, _ = db.insert_post(
        text=text, source=source, filter_passed=passed, filter_reasons=["awal"]
    )
    return post_id


class TestCountByFilter:
    def test_empty_database(self, db):
        assert db.count_by_filter() == {"passed": 0, "hidden": 0, "unknown": 0}

    def test_counts_grouped(self, db):
        _insert(db, IPHONE_TEXT, passed=True)
        _insert(db, IPHONE_TEXT + " extra", passed=False)
        db.insert_post(text="iPhone 12 murah", source="manual")  # NULL = unknown
        assert db.count_by_filter() == {"passed": 1, "hidden": 1, "unknown": 1}

    def test_count_ai_analysed_zero_without_analysis(self, db):
        _insert(db, IPHONE_TEXT, passed=True)
        assert db.count_ai_analysed() == 0


class TestDeleteBySource:
    def test_only_demo_rows_removed(self, db):
        _insert(db, IPHONE_TEXT, passed=True, source="demo")
        _insert(db, IPHONE_TEXT + " dua", passed=True, source="demo")
        _insert(db, IPHONE_TEXT + " tiga", passed=True, source="manual")
        assert db.delete_posts_by_source("demo") == 2
        assert db.count_posts() == 1
        assert db.count_posts(source="manual") == 1

    def test_delete_when_none_returns_zero(self, db):
        assert db.delete_posts_by_source("demo") == 0


class TestReapplyFilters:
    def test_old_hidden_rows_become_visible(self, db):
        _insert(db, IPHONE_TEXT, passed=False)
        _insert(db, OTHER_TEXT, passed=False)
        summary = reapply_filters(db, FilterConfig(require_iphone=True))
        assert summary.checked == 2
        assert summary.passed == 1
        assert summary.changed == 1
        counts = db.count_by_filter()
        assert counts["passed"] == 1
        assert counts["hidden"] == 1

    def test_reapply_does_not_duplicate_rows(self, db):
        _insert(db, IPHONE_TEXT, passed=True)
        before = db.count_posts()
        reapply_filters(db, FilterConfig(require_iphone=True))
        reapply_filters(db, FilterConfig(require_iphone=True))
        assert db.count_posts() == before

    def test_relaxing_filter_reveals_rows_that_only_failed_criteria(self, db):
        _insert(db, IPHONE_TEXT, passed=False)
        _insert(db, OTHER_TEXT, passed=False)
        summary = reapply_filters(db, FilterConfig(require_iphone=False))
        # Baris iPhone tampil lagi; baris non-iPhone tetap tersembunyi karena
        # kategorinya "tidak relevan" (bukan karena aturan iPhone).
        assert summary.passed == 1
        assert summary.changed == 1
        assert db.count_by_filter()["hidden"] == 1
        hidden = db.list_posts(filter_passed=False)[0]
        assert any("tidak relevan" in reason.lower() for reason in hidden["filter_reasons"])

    def test_reapply_updates_reason_and_filter_flag(self, db):
        post_id = _insert(db, IPHONE_TEXT, passed=False)
        reapply_filters(db, FilterConfig(require_iphone=True))
        post = db.get_post(post_id)
        assert post["filter_passed"] == 1
        assert "awal" not in post["filter_reasons"]

    def test_reapply_limit_flag(self, db):
        for index in range(3):
            _insert(db, f"{IPHONE_TEXT} #{index}", passed=False)
        summary = reapply_filters(db, FilterConfig(require_iphone=True), limit=2)
        assert summary.checked == 2
        assert summary.limited is True


class TestPreviewFilterCounts:
    def test_preview_matches_evaluate(self, db):
        _insert(db, IPHONE_TEXT, passed=False)
        _insert(db, OTHER_TEXT, passed=False)
        config = FilterConfig(require_iphone=True)
        passed, checked = preview_filter_counts(db, config, limit=10)
        expected = sum(
            1
            for text in (IPHONE_TEXT, OTHER_TEXT)
            if evaluate(text, config).passed
        )
        assert (passed, checked) == (expected, 2)

    def test_preview_does_not_modify_data(self, db):
        _insert(db, IPHONE_TEXT, passed=False)
        preview_filter_counts(db, FilterConfig(require_iphone=True), limit=10)
        assert db.count_by_filter()["hidden"] == 1
