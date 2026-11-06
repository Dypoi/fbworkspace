"""Smoke test dashboard Streamlit memakai AppTest (tanpa browser).

Tujuan: memastikan app.py benar-benar bisa dirender tanpa exception,
termasuk saat database kosong dan setelah data DEMO dimuat.
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


def test_app_renders_without_exception(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert not at.exception, [str(e) for e in at.exception]
    titles = [t.value for t in at.title]
    assert any("FB Workspace" in t for t in titles)


def test_app_shows_demo_prompt_on_empty_db(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    assert any("DEMO" in str(info.value) for info in at.info)


def test_app_has_all_tabs(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    labels = [tab.label for tab in at.tabs]
    for expected in ("Jalankan", "Input Manual", "Postingan", "Analisis Ollama", "Pengaturan", "Bantuan"):
        assert any(expected in label for label in labels), expected


def test_demo_button_loads_data_and_app_still_renders(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch).run()
    at.button(key="load_demo_main").click().run()
    assert not at.exception, [str(e) for e in at.exception]
    # Setelah data demo dimuat, tabel postingan harus muncul
    assert len(at.dataframe) >= 1
