"""Smoke test dashboard Streamlit (alur 3 langkah) memakai AppTest, tanpa browser.

Tujuan: memastikan app.py bisa dirender tanpa exception, punya empat tab
(Mulai/Hasil/Balas/Pengaturan), dan segmentasi hasil benar-benar ada.
"""

from __future__ import annotations

from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _fresh_app(tmp_path: Path, monkeypatch) -> AppTest:
    monkeypatch.setenv("FBW_DB_PATH", str(tmp_path / "smoke.db"))
    monkeypatch.setenv("FBW_OLLAMA_ENABLED", "false")
    return AppTest.from_file(APP, default_timeout=60)


def _by_key(elements, key: str):
    return [element for element in elements if getattr(element, "key", None) == key]


def test_app_renders_without_exception(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert not at.exception, [str(e) for e in at.exception]
    titles = [t.value for t in at.title]
    assert any("FB Workspace" in t for t in titles)


def test_app_shows_demo_prompt_on_empty_db(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert any("DEMO" in str(info.value) for info in at.info)
    assert _by_key(at.button, "onboarding_demo"), "Tombol muat DEMO pada onboarding hilang"


def test_app_has_four_tabs(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    labels = [tab.label for tab in at.tabs]
    assert len(labels) == 4, labels
    for expected in ("Mulai", "Hasil", "Balas", "Pengaturan"):
        assert any(expected in label for label in labels), expected
    for removed in ("Input Manual", "Analisis Ollama", "Bantuan", "Jalankan"):
        assert not any(removed in label for label in labels), removed


def test_balas_tab_runs_dry_by_default(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    dry = _by_key(at.checkbox, "fbw_msg_dry")
    assert dry and dry[0].value is True, "Uji kering harus aktif secara bawaan"
    start = _by_key(at.button, "fbw_start_messenger")
    assert start, "Tombol jalankan balas DM tidak ada"
    assert start[0].disabled is True, "Tombol harus mati sampai pernyataan dicentang"


def test_results_tab_has_segmented_control(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    radio = _by_key(at.radio, "fbw_view")
    assert radio, "Segmented control 'Semua/Lolos/Tersembunyi' tidak ditemukan"
    options = list(radio[0].options)
    assert any("Semua" in option for option in options)
    assert any("Lolos" in option for option in options)
    assert any("Tersembunyi" in option for option in options)
    # Checkbox lama yang menyembunyikan data harus hilang
    assert not _by_key(at.checkbox, "fbw_only_passed")


def test_demo_button_loads_data_and_app_still_renders(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    at.button(key="onboarding_demo").click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert len(at.dataframe) >= 1
    # Banner DEMO muncul karena database kini berisi data sintetis
    assert any("DEMO" in str(w.value) for w in at.warning)


def test_reapply_filter_button_available_after_demo(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    at.button(key="onboarding_demo").click().run()
    buttons = _by_key(at.button, "reapply_filters")
    assert buttons, "Tombol 'Terapkan ulang filter' tidak ada di tab Hasil"
    buttons[0].click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("Selesai" in str(s.value) for s in at.success)


def test_comment_rows_are_flagged_and_cleanable(tmp_path, monkeypatch):
    from fbworkspace.db import Database

    db_path = tmp_path / "junk.db"
    monkeypatch.setenv("FBW_DB_PATH", str(db_path))
    monkeypatch.setenv("FBW_OLLAMA_ENABLED", "false")
    seed = Database(db_path)
    seed.insert_post(
        text="Ali Romlan · 1hRam brpLikeReplyShare", source="browser", filter_passed=False
    )
    seed.insert_post(
        text="Dijual iPhone X 64GB mulus, 5jt COD Tangerang",
        source="browser",
        filter_passed=True,
    )

    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("komentar" in str(w.value).lower() for w in at.warning)

    assert _by_key(at.button, "del_suspect"), "Tombol hapus baris komentar tidak ada"
    at.checkbox(key="del_suspect_confirm").check().run()
    at.button(key="del_suspect").click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert Database(db_path).count_posts() == 1


def test_messenger_panel_with_zero_chats_renders_without_duplicate_key(tmp_path, monkeypatch):
    """Regresi nyata: 0 chat + panel dirender dua kali -> StreamlitDuplicateElementKey."""
    from fbworkspace.db import Database

    db_path = tmp_path / "messenger.db"
    monkeypatch.setenv("FBW_DB_PATH", str(db_path))
    monkeypatch.setenv("FBW_OLLAMA_ENABLED", "false")
    seed = Database(db_path)
    job_id = seed.create_job(
        kind="messenger",
        group_urls=[],
        config={"dry_run": True, "max_threads": 5, "max_replies": 5},
    )
    seed.update_job(
        job_id,
        status="diblokir/tidak diizinkan",
        message="Tidak ada chat yang terbaca di Messenger.",
        counters_json='{"threads": 0, "messages": 0, "replies": 0}',
        diagnostics_json=(
            '{"url": "https://www.facebook.com/messages/", "title": "Messenger",'
            ' "thread_links": 1, "chat_links": 13, "rows": 13, "composer": "",'
            ' "text": "Chats"}'
        ),
    )

    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [str(e) for e in at.exception]
    assert _by_key(at.button, f"msg_diag_{job_id}"), "Tombol periksa inbox tidak muncul"
    assert any("tautan chat" in str(e.value).lower() for e in at.error) or any(
        "13" in str(e.value) for e in at.error
    )


def test_watch_mode_controls_exist_and_default_on(tmp_path, monkeypatch):
    monkeypatch.setenv("FBW_DB_PATH", str(tmp_path / "watch.db"))
    monkeypatch.setenv("FBW_OLLAMA_ENABLED", "false")
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [str(e) for e in at.exception]
    watch = at.checkbox(key="fbw_msg_watch")
    assert watch.value is True, "Mode pantau seharusnya aktif secara bawaan"
    assert "Pantau terus" in watch.label


def test_stop_button_visible_for_active_messenger_job(tmp_path, monkeypatch):
    """Tombol berhenti harus ada di panel job (bukan hanya di tab feed)."""
    import json
    from datetime import datetime, timezone

    from fbworkspace.db import Database

    db_path = tmp_path / "stop.db"
    db = Database(db_path)
    job_id = db.create_job(
        kind="messenger",
        group_urls=[],
        config={"dry_run": False, "watch": True, "max_cycles": 0},
    )
    db.update_job(
        job_id,
        status="berjalan",
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        counters_json=json.dumps({"threads": 1, "cycles": 2, "replies": 1}),
    )

    monkeypatch.setenv("FBW_DB_PATH", str(db_path))
    monkeypatch.setenv("FBW_OLLAMA_ENABLED", "false")
    monkeypatch.setattr("fbworkspace.jobs.tail_log", lambda *a, **k: "")

    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [str(e) for e in at.exception]

    # panel live ada di tab Mulai/Balas; tombol harus bisa diklik
    buttons = [b for b in at.button if "Hentikan" in (b.label or "")]
    assert buttons, "Tombol ⛔ Hentikan worker tidak ditemukan"


def test_force_stop_uses_pid_after_dashboard_restart(tmp_path, monkeypatch):
    """Setelah dashboard di-restart, worker tetap hidup: PID dari DB harus dipakai."""
    import os

    from fbworkspace import jobs
    from fbworkspace.db import Database

    db = Database(tmp_path / "force.db")
    job_id = db.create_job(kind="messenger", group_urls=[], config={})
    db.update_job(job_id, status="berjalan", pid=os.getpid())

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_pid_is_our_worker", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)), raising=False)

    note = jobs.force_stop(db, job_id, None)  # tidak ada Popen: sesi dashboard baru

    assert "dihentikan paksa" in note.lower()
    assert killed, "PID worker seharusnya dihentikan"
    assert db.get_job(job_id)["status"] == "dihentikan"
    assert db.is_stop_requested(job_id) is True


def test_force_stop_never_kills_unknown_pid(tmp_path, monkeypatch):
    """PID yang jelas bukan worker fbworkspace tidak boleh dibunuh."""
    from fbworkspace import jobs
    from fbworkspace.db import Database

    db = Database(tmp_path / "force3.db")
    job_id = db.create_job(kind="messenger", group_urls=[], config={})
    db.update_job(job_id, status="berjalan", pid=999999)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_pid_command_line", lambda pid: "notepad.exe")
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)), raising=False)

    jobs.force_stop(db, job_id, None)

    assert killed == [], "proses asing tidak boleh dibunuh"
    assert db.get_job(job_id)["status"] == "dihentikan"


def test_pid_is_our_worker_matches_app_process(monkeypatch):
    from fbworkspace import jobs

    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        jobs,
        "_pid_command_line",
        lambda pid: r'"C:\x\python.exe" -m fbworkspace.messenger_worker --job 3',
    )
    assert jobs._pid_is_our_worker(1234) is True

    monkeypatch.setattr(jobs, "_pid_command_line", lambda pid: r"C:\Windows\notepad.exe")
    assert jobs._pid_is_our_worker(1234) is False


def test_force_stop_without_pid_marks_stopped(tmp_path):
    from fbworkspace import jobs
    from fbworkspace.db import Database

    db = Database(tmp_path / "force2.db")
    job_id = db.create_job(kind="messenger", group_urls=[], config={})
    db.update_job(job_id, status="berjalan")

    jobs.force_stop(db, job_id, None)
    assert db.get_job(job_id)["status"] == "dihentikan"


def test_cancel_stop_clears_request(tmp_path):
    from fbworkspace.db import Database

    db = Database(tmp_path / "cancel.db")
    job_id = db.create_job(kind="messenger", group_urls=[], config={})
    db.request_stop(job_id)
    assert db.is_stop_requested(job_id) is True

    db.clear_stop_request(job_id)
    assert db.is_stop_requested(job_id) is False


def test_pid_alive_never_raises(monkeypatch):
    import sys
    """Windows: os.kill(pid, 0) melempar SystemError [WinError 87] — jangan crash."""
    from fbworkspace import jobs

    def boom(pid, sig):
        raise SystemError("<built-in function kill> returned a result with an exception")

    monkeypatch.setattr("os.kill", boom, raising=False)
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    assert jobs._pid_alive(1234) is False

    monkeypatch.setattr(jobs.sys, "platform", "win32")

    class FakeKernel32:
        def OpenProcess(self, *a):
            raise OSError("[WinError 87] The parameter is incorrect")

    class FakeCtypes:
        windll = type("W", (), {"kernel32": FakeKernel32()})()

    monkeypatch.setitem(sys.modules, "ctypes", FakeCtypes)
    assert jobs._pid_alive(1234) is False


def test_force_stop_reports_failure_without_raising(tmp_path, monkeypatch):
    from fbworkspace import jobs
    from fbworkspace.db import Database

    db = Database(tmp_path / "fs.db")
    job_id = db.create_job(kind="messenger", group_urls=[], config={})
    db.update_job(job_id, status="berjalan", pid=4242)

    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_pid_is_our_worker", lambda pid: True)
    monkeypatch.setattr(jobs, "_terminate_pid", lambda pid: (False, "ditolak Windows"))

    note = jobs.force_stop(db, job_id, None)
    assert "Gagal menghentikan" in note

    monkeypatch.setattr(jobs, "_terminate_pid", lambda pid: (True, "dihentikan"))
    assert "dihentikan paksa" in jobs.force_stop(db, job_id, None).lower()


def test_code_version_is_reported(tmp_path):
    """Versi kode harus terlihat, supaya jelas apakah worker memakai versi terbaru."""
    from fbworkspace.dashboard import _code_version
    from fbworkspace.messenger_worker import _code_version as worker_version

    for value in (_code_version(), worker_version()):
        assert isinstance(value, str)
        assert value
