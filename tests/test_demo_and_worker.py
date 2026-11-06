"""Uji data DEMO dan logika status worker (tanpa membuka browser)."""

from __future__ import annotations

from pathlib import Path

from fbworkspace.browser.base import CollectOutcome
from fbworkspace.browser.manual import ManualAdapter
from fbworkspace.demo import DEMO_PATH, load_demo_posts
from fbworkspace.models import AccessStatus, CollectLimits, FilterConfig
from fbworkspace.worker import _adapter_for, _map_outcome_status


class TestDemoData:
    def test_demo_file_exists_and_marked(self):
        assert DEMO_PATH.exists()
        content = DEMO_PATH.read_text(encoding="utf-8")
        assert "DEMO" in content
        assert "sintetis" in content.lower()

    def test_load_is_idempotent(self, db):
        first = load_demo_posts(db, FilterConfig())
        assert first["ok"]
        assert first["inserted"] > 0
        second = load_demo_posts(db, FilterConfig())
        assert second["inserted"] == 0
        assert second["duplicates"] == first["inserted"]

    def test_demo_rows_are_labelled(self, db):
        load_demo_posts(db, FilterConfig())
        rows = db.list_posts(source="demo")
        assert rows
        assert all(row["source"] == "demo" for row in rows)
        assert all("[DEMO]" in row["text"] for row in rows)

    def test_demo_has_mix_of_relevance(self, db):
        load_demo_posts(db, FilterConfig())
        rows = db.list_posts()
        categories = {row["category"] for row in rows}
        assert "menjual" in categories
        assert "mencari" in categories
        assert "tidak relevan" in categories

    def test_missing_file_handled(self, db, tmp_path: Path):
        result = load_demo_posts(db, FilterConfig(), path=tmp_path / "tidak-ada.json")
        assert not result["ok"]
        assert "tidak ditemukan" in result["message"]


class TestAdapterSelection:
    def test_manual_mode(self, settings=None):
        class S:
            browser_profile_dir = Path("data/browser_profile")
            browser_headless = False
            browser_slow_mo_ms = 0
            login_wait_seconds = 180

        adapter = _adapter_for("manual", S())
        assert isinstance(adapter, ManualAdapter)

    def test_browser_mode_returns_playwright_adapter(self):
        from fbworkspace.browser.playwright_fb import PlaywrightFacebookAdapter

        class S:
            browser_profile_dir = Path("data/browser_profile")
            browser_headless = False
            browser_slow_mo_ms = 0
            login_wait_seconds = 180

        adapter = _adapter_for("playwright", S())
        assert isinstance(adapter, PlaywrightFacebookAdapter)
        assert adapter.login_wait_seconds == 180

    def test_manual_adapter_does_nothing(self):
        outcome = ManualAdapter().collect(
            type("T", (), {"group_urls": [], "limits": CollectLimits()})()
        )
        assert outcome.status == AccessStatus.OK_EMPTY
        assert outcome.posts == []


class TestStatusMapping:
    def test_ok_maps_to_completed(self):
        status, _message = _map_outcome_status(
            CollectOutcome(AccessStatus.OK, [], "ok"), {}
        )
        assert status == "selesai"

    def test_blocked_maps_to_blocked(self):
        for access in (
            AccessStatus.LOGIN_REQUIRED,
            AccessStatus.CHECKPOINT,
            AccessStatus.PERMISSION_DENIED,
            AccessStatus.RATE_LIMITED,
        ):
            status, message = _map_outcome_status(CollectOutcome(access, [], "detail"), {})
            assert status == "diblokir/tidak diizinkan"
            assert message == "detail"

    def test_stopped_and_failed(self):
        assert _map_outcome_status(CollectOutcome(AccessStatus.STOPPED), {})[0] == "dihentikan"
        assert _map_outcome_status(CollectOutcome(AccessStatus.ERROR), {})[0] == "gagal"

    def test_partial_results_are_kept_on_block(self):
        from fbworkspace.models import RawPost

        outcome = CollectOutcome(
            status=AccessStatus.CHECKPOINT,
            posts=[RawPost(text="iPhone 13 8jt", group_url="https://x")],
            message="checkpoint",
        )
        assert len(outcome.posts) == 1
        assert not outcome.stopped_normally


class TestLimits:
    def test_clamp_respects_hard_max(self):
        limits = CollectLimits(max_posts=500, max_seconds=99999).clamp(hard_max=25)
        assert limits.max_posts == 25
        assert limits.max_seconds <= 3600

    def test_clamp_minimums(self):
        limits = CollectLimits(max_posts=0, max_seconds=0).clamp()
        assert limits.max_posts == 1
        assert limits.max_seconds == 10
