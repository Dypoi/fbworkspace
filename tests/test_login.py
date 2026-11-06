"""Uji deteksi sesi, penantian login, dan marker status (tanpa browser)."""

from __future__ import annotations

import json
from pathlib import Path

from fbworkspace.browser.session import (
    LOGIN_STATUS_LABELS,
    LoginMarker,
    cookie_names,
    has_fb_session,
    is_terminal_status,
    read_login_marker,
    wait_for_login,
    write_login_marker,
)
from fbworkspace.login_session import default_marker_path


class TestSessionCookie:
    def test_c_user_means_logged_in(self):
        assert has_fb_session([{"name": "c_user", "value": "123"}])

    def test_other_cookies_do_not_count(self):
        assert not has_fb_session([{"name": "datr", "value": "x"}])

    def test_empty_value_does_not_count(self):
        assert not has_fb_session([{"name": "c_user", "value": ""}])

    def test_none_and_empty(self):
        assert not has_fb_session(None)
        assert not has_fb_session([])

    def test_ignores_garbage_entries(self):
        assert not has_fb_session(["bukan dict", None, {"value": "x"}])

    def test_cookie_names_are_sorted_and_unique(self):
        assert cookie_names([{"name": "b"}, {"name": "a"}, {"name": "a"}]) == ["a", "b"]

    def test_cookie_names_empty(self):
        assert cookie_names(None) == []


class _FakeClock:
    """Jam palsu: sleep() memajukan waktu, sehingga tes tidak benar-benar menunggu."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestWaitForLogin:
    def test_detects_immediately(self):
        clock = _FakeClock()
        result = wait_for_login(
            cookie_provider=lambda: [{"name": "c_user", "value": "1"}],
            timeout_seconds=10,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert result.detected
        assert result.reason == "login"
        assert clock.slept == []

    def test_timeout_when_never_logged_in(self):
        clock = _FakeClock()
        result = wait_for_login(
            cookie_provider=lambda: [],
            timeout_seconds=5,
            poll_interval=2,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert not result.detected
        assert result.reason == "timeout"
        # Tidak pernah tidur melebihi sisa waktu
        assert sum(clock.slept) <= 5 + 0.001

    def test_detects_after_several_polls(self):
        clock = _FakeClock()
        state = {"calls": 0}

        def provider():
            state["calls"] += 1
            return [{"name": "c_user", "value": "1"}] if state["calls"] >= 3 else []

        result = wait_for_login(
            cookie_provider=provider,
            timeout_seconds=30,
            poll_interval=2,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert result.detected
        assert state["calls"] == 3

    def test_stop_request_wins(self):
        clock = _FakeClock()
        result = wait_for_login(
            cookie_provider=lambda: [],
            timeout_seconds=60,
            should_stop=lambda: True,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert not result.detected
        assert result.reason == "stopped"

    def test_disabled_when_timeout_zero(self):
        result = wait_for_login(cookie_provider=lambda: [], timeout_seconds=0)
        assert not result.detected
        assert result.reason == "disabled"
        assert "dimatikan" in result.message()

    def test_provider_errors_do_not_crash(self):
        clock = _FakeClock()

        def provider():
            raise RuntimeError("browser ditutup")

        result = wait_for_login(
            cookie_provider=provider,
            timeout_seconds=4,
            poll_interval=2,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert not result.detected
        assert result.reason == "timeout"

    def test_on_tick_called_with_remaining(self):
        clock = _FakeClock()
        seen: list[int] = []
        wait_for_login(
            cookie_provider=lambda: [],
            timeout_seconds=4,
            poll_interval=2,
            sleep=clock.sleep,
            clock=clock.clock,
            on_tick=seen.append,
        )
        assert seen and seen[0] <= 4

    def test_message_for_detected(self):
        result = wait_for_login(
            cookie_provider=lambda: [{"name": "c_user", "value": "1"}],
            timeout_seconds=5,
        )
        assert "Login terdeteksi" in result.message()


class TestMarkerFile:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "login.json"
        write_login_marker(path, LoginMarker(status="menunggu", profile_dir="x"))
        data = read_login_marker(path)
        assert data["status"] == "menunggu"
        assert data["profile_dir"] == "x"
        assert data["message"] == LOGIN_STATUS_LABELS["menunggu"]
        assert data["updated_at"]

    def test_overwrite_is_atomic_and_readable(self, tmp_path: Path):
        path = tmp_path / "login.json"
        write_login_marker(path, LoginMarker(status="menunggu"))
        write_login_marker(path, LoginMarker(status="selesai", message="beres"))
        data = read_login_marker(path)
        assert data["status"] == "selesai"
        assert data["message"] == "beres"
        assert not path.with_suffix(".tmp").exists()

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_login_marker(tmp_path / "tidak-ada.json") == {}

    def test_corrupt_file_returns_empty(self, tmp_path: Path):
        path = tmp_path / "login.json"
        path.write_text("{bukan json", encoding="utf-8")
        assert read_login_marker(path) == {}

    def test_non_dict_json_returns_empty(self, tmp_path: Path):
        path = tmp_path / "login.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert read_login_marker(path) == {}

    def test_terminal_statuses(self):
        assert is_terminal_status("selesai")
        assert is_terminal_status("error")
        assert not is_terminal_status("menunggu")
        assert not is_terminal_status(None)

    def test_default_marker_path_in_logs(self):
        path = default_marker_path()
        assert path.name == "login_session.json"
        assert path.parent.name == "logs"
