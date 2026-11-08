"""Uji penyimpanan SQLite, deduplikasi, status, job, dan penghapusan."""

from __future__ import annotations

import pytest

from fbworkspace.db import Database
from fbworkspace.models import UNKNOWN


class TestInsertAndRetrieve:
    def test_insert_returns_new_id(self, db):
        post_id, is_new = db.insert_post(text="Dijual iPhone 13 mulus", source="manual")
        assert is_new
        assert post_id > 0
        post = db.get_post(post_id)
        assert post["text"] == "Dijual iPhone 13 mulus"
        assert post["source"] == "manual"
        assert post["workflow_status"] == "baru"

    def test_empty_text_rejected(self, db):
        with pytest.raises(ValueError):
            db.insert_post(text="   ")

    def test_missing_link_is_none_not_empty_string(self, db):
        post_id, _ = db.insert_post(text="iPhone 12 murah")
        assert db.get_post(post_id)["post_url"] is None

    def test_unknown_time_precision_default(self, db):
        post_id, _ = db.insert_post(text="iPhone 12 murah")
        assert db.get_post(post_id)["posted_at_precision"] == UNKNOWN


class TestDeduplication:
    def test_same_post_link_is_duplicate(self, db):
        url = "https://www.facebook.com/groups/1/posts/999"
        first_id, first_new = db.insert_post(text="iPhone 13 8jt", post_url=url)
        second_id, second_new = db.insert_post(
            text="iPhone 13 8jt (teks berbeda)", post_url=url
        )
        assert first_new is True
        assert second_new is False
        assert first_id == second_id
        assert db.count_posts() == 1

    def test_tracking_params_do_not_create_duplicate(self, db):
        base = "https://www.facebook.com/groups/1/posts/777"
        db.insert_post(text="iPhone 11 murah", post_url=base)
        _id, is_new = db.insert_post(
            text="iPhone 11 murah", post_url=base + "?__cft__=xyz&ref=1"
        )
        assert not is_new

    def test_same_text_without_link_is_duplicate(self, db):
        _id1, new1 = db.insert_post(text="Cari iPhone 11 budget 4jt")
        _id2, new2 = db.insert_post(text="cari  iphone 11   budget 4jt")
        assert new1 is True
        assert new2 is False

    def test_limitation_edited_text_is_treated_as_new(self, db):
        _id1, new1 = db.insert_post(text="Cari iPhone 11 budget 4jt")
        _id2, new2 = db.insert_post(text="Cari iPhone 11 budget 4jt ya")
        # Keterbatasan yang didokumentasikan: tanpa ID tautan, teks yang diedit
        # dianggap postingan berbeda.
        assert new1 and new2
        assert db.count_posts() == 2

    def test_browser_and_manual_same_text_dedupe(self, db):
        _id1, new1 = db.insert_post(text="iPhone 12 64gb 5jt", source="manual")
        _id2, new2 = db.insert_post(text="iPhone 12 64gb 5jt", source="browser")
        assert new1 is True
        assert new2 is False


class TestStatusAndAnalysis:
    def test_set_status(self, db):
        post_id, _ = db.insert_post(text="iPhone 13")
        db.set_workflow_status(post_id, "relevan")
        assert db.get_post(post_id)["workflow_status"] == "relevan"

    def test_invalid_status_rejected(self, db):
        post_id, _ = db.insert_post(text="iPhone 13")
        with pytest.raises(ValueError):
            db.set_workflow_status(post_id, "entah")

    def test_save_and_update_analysis(self, db):
        post_id, _ = db.insert_post(text="iPhone 13")
        db.save_analysis(
            post_id,
            {
                "engine": "rule",
                "category": "menjual",
                "iphone_model": "iPhone 13",
                "price_amount": 8000000,
                "price_kind": "harga",
                "location": "Bandung",
                "details": {"storage": "128 GB"},
                "relevance_score": 90,
                "reasons": ["+40 menyebut iPhone."],
                "snippet": "iPhone 13 128GB",
            },
        )
        analysis = db.get_analysis(post_id)
        assert analysis["category"] == "menjual"
        assert analysis["details"]["storage"] == "128 GB"
        assert db.get_post(post_id)["analysis_status"] == "dianalisis (aturan)"

        # Upsert tidak membuat baris baru
        db.save_analysis(post_id, {"engine": "ollama", "category": "diskusi"})
        assert db.get_post(post_id)["analysis_status"] == "dianalisis (Ollama)"
        assert db.get_analysis(post_id)["category"] == "diskusi"

    def test_list_posts_search_and_filters(self, db):
        db.insert_post(text="Dijual iPhone 13 di Bandung", source="manual")
        db.insert_post(text="Cari iPhone 11 budget 4jt", source="demo")
        assert len(db.list_posts(search="Bandung")) == 1
        assert len(db.list_posts(source="demo")) == 1
        assert len(db.list_posts()) == 2

    def test_filter_passed_column(self, db):
        db.insert_post(text="iPhone 13", filter_passed=True, filter_reasons=["ok"])
        db.insert_post(text="Kulkas", filter_passed=False, filter_reasons=["tidak relevan"])
        assert len(db.list_posts(filter_passed=True)) == 1
        assert len(db.list_posts(filter_passed=False)) == 1


class TestJobs:
    def test_job_lifecycle(self, db):
        job_id = db.create_job(kind="browser", group_urls=["https://x"], config={"mode": "manual"})
        job = db.get_job(job_id)
        assert job["status"] == "antre"
        assert job["config"]["mode"] == "manual"

        db.update_job(job_id, status="berjalan", message="mulai")
        assert db.active_job()["id"] == job_id
        assert not db.is_stop_requested(job_id)

        db.request_stop(job_id)
        assert db.is_stop_requested(job_id)

        db.update_job(job_id, status="selesai")
        assert db.active_job() is None

    def test_unknown_job_column_rejected(self, db):
        job_id = db.create_job(kind="x", group_urls=[], config={})
        with pytest.raises(ValueError):
            db.update_job(job_id, tidak_ada=1)

    def test_events_order_and_filter(self, db):
        job_id = db.create_job(kind="x", group_urls=[], config={})
        db.log_event(job_id, "info", "satu")
        db.log_event(job_id, "warning", "dua")
        events = db.get_events(job_id)
        assert [e["message"] for e in events] == ["satu", "dua"]


class TestDeletion:
    def test_delete_post(self, db):
        post_id, _ = db.insert_post(text="iPhone 13")
        db.save_analysis(post_id, {"engine": "rule", "category": "menjual"})
        db.delete_post(post_id)
        assert db.get_post(post_id) is None
        assert db.get_analysis(post_id) is None

    def test_delete_all(self, db):
        db.insert_post(text="iPhone 13")
        db.insert_post(text="iPhone 11")
        db.delete_all_posts()
        assert db.count_posts() == 0

    def test_delete_jobs_and_events(self, db):
        job_id = db.create_job(kind="x", group_urls=[], config={})
        db.log_event(job_id, "info", "halo")
        db.delete_jobs_and_events()
        assert db.get_events(job_id) == []
        assert db.latest_job() is None


class TestStats:
    def test_stats_structure(self, db):
        db.insert_post(text="iPhone 13", source="demo")
        stats = db.stats()
        assert stats["total"] == 1
        assert stats["by_source"]["demo"] == 1
        assert "db_path" in stats


class TestSchemaMigration:
    """Database lama (dibuat sebelum tab Balas) harus tetap bisa dibuka."""

    def test_old_jobs_table_gets_new_columns(self, tmp_path):
        import sqlite3

        path = tmp_path / "lama.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                group_urls_json TEXT NOT NULL DEFAULT '[]',
                config_json TEXT NOT NULL DEFAULT '{}',
                message TEXT NOT NULL DEFAULT '',
                counters_json TEXT NOT NULL DEFAULT '{}',
                stop_requested INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, heartbeat_at TEXT
            );
            INSERT INTO jobs(status, created_at) VALUES('selesai','2026-11-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

        db = Database(path)
        old = db.get_job(1)
        assert old["status"] == "selesai"
        assert old["kind"] == "browser"
        assert old["diagnostics"] == {}

        job_id = db.create_job(kind="messenger", group_urls=[], config={})
        db.update_job(job_id, diagnostics_json='{"threads": 0}')
        assert db.get_job(job_id)["diagnostics"] == {"threads": 0}

    def test_init_is_idempotent(self, tmp_path):
        path = tmp_path / "dua.db"
        Database(path)
        Database(path)  # tidak boleh error karena kolom sudah ada
        assert Database(path).list_prices()
