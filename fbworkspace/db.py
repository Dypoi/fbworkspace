"""Lapisan database SQLite untuk FB Workspace.

Semua akses data lewat sini. Tidak ada data yang dikirim ke internet.

Catatan konkurensi:
- Dashboard (Streamlit) dan worker browser adalah dua proses berbeda.
- SQLite dijalankan mode WAL + busy_timeout supaya penulisan bersamaan aman.
- Setiap fungsi membuka koneksi sendiri yang singkat (tidak menyimpan koneksi global).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    UNKNOWN,
    AnalysisStatus,
    Source,
    WorkflowStatus,
    utcnow_iso,
)
from .normalize import derive_dedupe_key, normalize_text
from .pricelist import PriceEntry, default_entries

SCHEMA_VERSION = "3"

# Kolom job yang ditambahkan setelah skema awal. Dibuat lewat ALTER TABLE
# supaya database lama (milik pengguna) tetap bisa dibuka tanpa migrasi manual.
_JOB_EXTRA_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'browser'"),
    ("diagnostics_json", "TEXT NOT NULL DEFAULT '{}'"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    group_url           TEXT NOT NULL DEFAULT '',
    post_url            TEXT,
    text                TEXT NOT NULL,
    text_hash           TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL UNIQUE,
    dedupe_method       TEXT NOT NULL,
    found_at            TEXT NOT NULL,
    posted_at           TEXT,
    posted_at_raw       TEXT,
    posted_at_precision TEXT NOT NULL DEFAULT 'tidak diketahui',
    source              TEXT NOT NULL,
    filter_passed       INTEGER,
    filter_reasons_json TEXT NOT NULL DEFAULT '[]',
    analysis_status     TEXT NOT NULL DEFAULT 'belum dianalisis',
    workflow_status     TEXT NOT NULL DEFAULT 'baru',
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_found ON posts(found_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_workflow ON posts(workflow_status);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source);

CREATE TABLE IF NOT EXISTS analyses (
    post_id         INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    engine          TEXT NOT NULL,
    category        TEXT NOT NULL,
    iphone_model    TEXT NOT NULL DEFAULT 'tidak diketahui',
    price_amount    INTEGER,
    price_kind      TEXT NOT NULL DEFAULT 'tidak diketahui',
    location        TEXT NOT NULL DEFAULT 'tidak diketahui',
    details_json    TEXT NOT NULL DEFAULT '{}',
    relevance_score INTEGER NOT NULL DEFAULT 0,
    reasons_json    TEXT NOT NULL DEFAULT '[]',
    snippet         TEXT NOT NULL DEFAULT '',
    raw_output      TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL,
    group_urls_json TEXT NOT NULL DEFAULT '[]',
    config_json    TEXT NOT NULL DEFAULT '{}',
    message        TEXT NOT NULL DEFAULT '',
    counters_json  TEXT NOT NULL DEFAULT '{}',
    stop_requested INTEGER NOT NULL DEFAULT 0,
    pid            INTEGER,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    heartbeat_at   TEXT,
    diagnostics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS price_list (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key  TEXT NOT NULL,
    label      TEXT NOT NULL,
    storage    INTEGER,
    price      INTEGER NOT NULL,
    ready      INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(model_key, storage)
);

CREATE TABLE IF NOT EXISTS chats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key      TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_checked_at TEXT,
    last_reply_at   TEXT,
    last_reply_text TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL,
    msg_key    TEXT NOT NULL,
    direction  TEXT NOT NULL DEFAULT 'in',
    author     TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answered   INTEGER NOT NULL DEFAULT 0,
    reply_text TEXT NOT NULL DEFAULT '',
    replied_at TEXT,
    UNIQUE(thread_key, msg_key)
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_key, id);

CREATE TABLE IF NOT EXISTS escalations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    chat_name  TEXT NOT NULL DEFAULT '',
    chat_url   TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'antre',
    attempts   INTEGER NOT NULL DEFAULT 0,
    channel    TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_escalations_status ON escalations(status, id);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    # ------------------------------------------------------------------ dasar
    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        connection = _connect(self.path)
        try:
            yield connection
        finally:
            connection.close()

    def init(self) -> None:
        with self.conn() as conn:
            conn.executescript(_SCHEMA)
            self._ensure_columns(conn)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
        self.seed_prices_if_empty()
        # Migrasi perbaikan: buang tanda "terjawab" tanpa balasan (uji kering lama).
        try:
            self.reset_unanswered_marks()
        except Exception:  # noqa: BLE001 - jangan gagalkan pembukaan database
            pass

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """Tambahkan kolom baru ke database lama (idempoten, tanpa kehilangan data)."""
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, definition in _JOB_EXTRA_COLUMNS:
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                # Kolom sudah ada (proses lain menambahkannya lebih dulu).
                continue

    # ---------------------------------------------------------------- postingan
    def insert_post(
        self,
        *,
        text: str,
        group_url: str = "",
        post_url: str | None = None,
        source: str = Source.MANUAL.value,
        posted_at_raw: str | None = None,
        posted_at: str | None = None,
        posted_at_precision: str = UNKNOWN,
        found_at: str | None = None,
        filter_passed: bool | None = None,
        filter_reasons: list[str] | None = None,
    ) -> tuple[int, bool]:
        """Simpan postingan.

        Kembalikan (post_id, baru_disimpan). Dedupe berdasarkan ID tautan,
        atau hash teks ternormalisasi bila tautan tidak tersedia.
        """
        clean_text = normalize_text(text)
        if not clean_text:
            raise ValueError("Teks postingan kosong setelah normalisasi.")

        dedupe_key, method = derive_dedupe_key(post_url, clean_text)
        now = utcnow_iso()
        text_hash = dedupe_key.split(":", 1)[1] if method == "text_hash" else ""
        with self.conn() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO posts (
                        group_url, post_url, text, text_hash, dedupe_key, dedupe_method,
                        found_at, posted_at, posted_at_raw, posted_at_precision,
                        source, filter_passed, filter_reasons_json, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        group_url or "",
                        post_url or None,
                        clean_text,
                        text_hash,
                        dedupe_key,
                        method,
                        found_at or now,
                        posted_at,
                        posted_at_raw,
                        posted_at_precision,
                        source,
                        None if filter_passed is None else int(bool(filter_passed)),
                        json.dumps(filter_reasons or [], ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                return int(cur.lastrowid), True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT id FROM posts WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if row is None:  # pragma: no cover - sangat jarang
                    raise
                return int(row["id"]), False

    def get_post(self, post_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute(
                """
                SELECT p.*, a.engine, a.category, a.iphone_model, a.price_amount,
                       a.price_kind, a.location, a.details_json, a.relevance_score,
                       a.reasons_json, a.snippet, a.analyzed_at, a.raw_output
                FROM posts p
                LEFT JOIN analyses a ON a.post_id = p.id
                WHERE p.id = ?
                """,
                (post_id,),
            ).fetchone()
        return _row_to_post(row) if row else None

    def list_posts(
        self,
        *,
        search: str = "",
        workflow_status: str | None = None,
        category: str | None = None,
        source: str | None = None,
        filter_passed: bool | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append("(p.text LIKE ? OR p.group_url LIKE ? OR IFNULL(p.post_url,'') LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if workflow_status:
            clauses.append("p.workflow_status = ?")
            params.append(workflow_status)
        if category:
            clauses.append("a.category = ?")
            params.append(category)
        if source:
            clauses.append("p.source = ?")
            params.append(source)
        if filter_passed is not None:
            clauses.append("p.filter_passed = ?")
            params.append(int(bool(filter_passed)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT p.*, a.engine, a.category, a.iphone_model, a.price_amount,
                   a.price_kind, a.location, a.details_json, a.relevance_score,
                   a.reasons_json, a.snippet, a.analyzed_at, a.raw_output
            FROM posts p
            LEFT JOIN analyses a ON a.post_id = p.id
            {where}
            ORDER BY p.found_at DESC, p.id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([max(1, min(int(limit), 5000)), max(0, int(offset))])
        with self.conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_post(r) for r in rows]

    def set_workflow_status(self, post_id: int, status: str) -> None:
        if status not in {s.value for s in WorkflowStatus}:
            raise ValueError(f"Status tidak dikenal: {status}")
        with self.conn() as conn:
            conn.execute(
                "UPDATE posts SET workflow_status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow_iso(), post_id),
            )

    def set_analysis_status(self, post_id: int, status: str) -> None:
        with self.conn() as conn:
            conn.execute(
                "UPDATE posts SET analysis_status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow_iso(), post_id),
            )

    def delete_post(self, post_id: int) -> None:
        with self.conn() as conn:
            conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    def delete_posts_by_source(self, source: str) -> int:
        """Hapus postingan berdasarkan sumber (mis. hanya data DEMO)."""
        with self.conn() as conn:
            cur = conn.execute("DELETE FROM posts WHERE source = ?", (source,))
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def find_suspicious_posts(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Baris lama yang teksnya berpola komentar/reply, bukan isi postingan."""
        from .normalize import looks_like_comment

        return [
            row for row in self.list_posts(limit=limit) if looks_like_comment(row["text"])
        ]

    def delete_suspicious_posts(self) -> int:
        """Hapus baris yang terdeteksi komentar/reply. Kembalikan jumlahnya."""
        ids = [row["id"] for row in self.find_suspicious_posts()]
        if not ids:
            return 0
        with self.conn() as conn:
            conn.executemany("DELETE FROM analyses WHERE post_id = ?", [(i,) for i in ids])
            conn.executemany("DELETE FROM posts WHERE id = ?", [(i,) for i in ids])
        return len(ids)

    def update_filter_result(self, post_id: int, passed: bool, reasons: list[str]) -> None:
        """Perbarui hasil filter tanpa menyentuh hasil analisis/AI."""
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE posts
                SET filter_passed = ?, filter_reasons_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    int(bool(passed)),
                    json.dumps(reasons or [], ensure_ascii=False),
                    utcnow_iso(),
                    post_id,
                ),
            )

    def count_ai_analysed(self) -> int:
        """Jumlah postingan yang dianalisis dengan Ollama (bukan aturan)."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM analyses WHERE engine = 'ollama'"
            ).fetchone()
        return int(row["c"]) if row else 0

    def count_by_filter(self) -> dict[str, int]:
        """Jumlah postingan per status filter: passed / hidden / unknown."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT filter_passed AS fp, COUNT(*) AS c FROM posts GROUP BY fp"
            ).fetchall()
        out = {"passed": 0, "hidden": 0, "unknown": 0}
        for row in rows:
            key = "passed" if row["fp"] == 1 else "hidden" if row["fp"] == 0 else "unknown"
            out[key] += int(row["c"])
        return out

    def delete_all_posts(self) -> int:
        with self.conn() as conn:
            cur = conn.execute("DELETE FROM posts")
            conn.execute("DELETE FROM events WHERE job_id IS NOT NULL")
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def count_posts(self, *, source: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM posts"
        params: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        with self.conn() as conn:
            return int(conn.execute(sql, params).fetchone()["c"])

    # ----------------------------------------------------------------- analisis
    def save_analysis(self, post_id: int, result: dict[str, Any], raw_output: str | None = None) -> None:
        now = utcnow_iso()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO analyses (
                    post_id, engine, category, iphone_model, price_amount, price_kind,
                    location, details_json, relevance_score, reasons_json, snippet,
                    raw_output, analyzed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET
                    engine=excluded.engine, category=excluded.category,
                    iphone_model=excluded.iphone_model, price_amount=excluded.price_amount,
                    price_kind=excluded.price_kind, location=excluded.location,
                    details_json=excluded.details_json,
                    relevance_score=excluded.relevance_score,
                    reasons_json=excluded.reasons_json, snippet=excluded.snippet,
                    raw_output=excluded.raw_output, analyzed_at=excluded.analyzed_at
                """,
                (
                    post_id,
                    result.get("engine", "rule"),
                    result.get("category", UNKNOWN),
                    result.get("iphone_model", UNKNOWN),
                    result.get("price_amount"),
                    result.get("price_kind", UNKNOWN),
                    result.get("location", UNKNOWN),
                    json.dumps(result.get("details") or {}, ensure_ascii=False),
                    int(result.get("relevance_score") or 0),
                    json.dumps(result.get("reasons") or [], ensure_ascii=False),
                    result.get("snippet") or "",
                    raw_output,
                    now,
                ),
            )
            status = (
                AnalysisStatus.OLLAMA.value
                if result.get("engine") == "ollama"
                else AnalysisStatus.RULE.value
            )
            conn.execute(
                "UPDATE posts SET analysis_status = ?, updated_at = ? WHERE id = ?",
                (status, now, post_id),
            )

    def get_analysis(self, post_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM analyses WHERE post_id = ?", (post_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["details"] = json.loads(data.pop("details_json") or "{}")
        data["reasons"] = json.loads(data.pop("reasons_json") or "[]")
        return data

    # -------------------------------------------------------------------- job
    def create_job(
        self, *, kind: str, group_urls: list[str], config: dict[str, Any], status: str = "antre"
    ) -> int:
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs (kind, status, group_urls_json, config_json, created_at)
                VALUES (?,?,?,?,?)
                """,
                (kind, status, json.dumps(group_urls), json.dumps(config), utcnow_iso()),
            )
            return int(cur.lastrowid)

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {
            "status", "message", "counters_json", "stop_requested", "pid",
            "started_at", "finished_at", "heartbeat_at", "diagnostics_json",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Kolom job tidak dikenal: {key}")
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(job_id)
        with self.conn() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def latest_job(self) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return _row_to_job(row) if row else None

    def active_job(self) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('antre','berjalan') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _row_to_job(row) if row else None

    def request_stop(self, job_id: int) -> None:
        with self.conn() as conn:
            conn.execute(
                "UPDATE jobs SET stop_requested = 1, message = ? WHERE id = ?",
                ("Permintaan berhenti diterima. Menunggu worker berhenti dengan rapi…", job_id),
            )
        self.log_event(job_id, "warning", "Pengguna menekan tombol berhenti.")

    def clear_stop_request(self, job_id: int) -> None:
        """Batalkan permintaan berhenti (dipakai bila worker dalam keadaan macet)."""
        with self.conn() as conn:
            conn.execute(
                "UPDATE jobs SET stop_requested = 0, message = ? WHERE id = ?",
                ("Permintaan berhenti dibatalkan. Worker melanjutkan pemantauan.", job_id),
            )
        self.log_event(job_id, "warning", "Pengguna membatalkan permintaan berhenti.")

    def is_stop_requested(self, job_id: int) -> bool:
        with self.conn() as conn:
            row = conn.execute("SELECT stop_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row["stop_requested"])

    # ------------------------------------------------------------------ event
    def log_event(self, job_id: int | None, level: str, message: str) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO events (job_id, ts, level, message) VALUES (?,?,?,?)",
                (job_id, utcnow_iso(), level, str(message)[:2000]),
            )

    def get_events(self, job_id: int | None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE job_id IS ? ORDER BY id DESC LIMIT ?"
        with self.conn() as conn:
            rows = conn.execute(sql, (job_id, max(1, min(limit, 1000)))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]


    # ------------------------------------------------------------- daftar harga
    def list_prices(self) -> list[PriceEntry]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM price_list ORDER BY model_key, storage"
            ).fetchall()
        return [
            PriceEntry(
                model_key=row["model_key"],
                label=row["label"],
                storage=row["storage"],
                price=int(row["price"]),
                ready=bool(row["ready"]),
            )
            for row in rows
        ]

    def price_rows(self) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM price_list ORDER BY model_key, storage"
            ).fetchall()
        return [dict(row) for row in rows]

    def seed_prices_if_empty(self) -> int:
        with self.conn() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM price_list").fetchone()["c"]
        if total:
            return 0
        entries = default_entries()
        self.replace_prices(entries)
        return len(entries)

    def replace_prices(self, entries: list[PriceEntry]) -> None:
        now = utcnow_iso()
        with self.conn() as conn:
            conn.execute("DELETE FROM price_list")
            conn.executemany(
                "INSERT INTO price_list(model_key, label, storage, price, ready, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (e.model_key, e.label, e.storage, e.price, int(e.ready), now)
                    for e in entries
                ],
            )

    def upsert_price(self, entry: PriceEntry) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO price_list(model_key, label, storage, price, ready, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(model_key, storage) DO UPDATE SET "
                "label=excluded.label, price=excluded.price, ready=excluded.ready, "
                "updated_at=excluded.updated_at",
                (entry.model_key, entry.label, entry.storage, entry.price, int(entry.ready), utcnow_iso()),
            )

    def set_price_ready(self, price_id: int, ready: bool) -> None:
        with self.conn() as conn:
            conn.execute(
                "UPDATE price_list SET ready=?, updated_at=? WHERE id=?",
                (int(ready), utcnow_iso(), int(price_id)),
            )

    def delete_price(self, price_id: int) -> None:
        with self.conn() as conn:
            conn.execute("DELETE FROM price_list WHERE id=?", (int(price_id),))

    # ------------------------------------------------------------------- chat
    def upsert_chat(self, thread_key: str, name: str = "", url: str = "") -> None:
        now = utcnow_iso()
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO chats(thread_key, name, url, created_at, updated_at, last_checked_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_key) DO UPDATE SET "
                "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE chats.name END, "
                "url=CASE WHEN excluded.url != '' THEN excluded.url ELSE chats.url END, "
                "updated_at=excluded.updated_at, last_checked_at=excluded.last_checked_at",
                (thread_key, name, url, now, now, now),
            )

    def get_chat(self, thread_key: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM chats WHERE thread_key=?", (thread_key,)).fetchone()
        return dict(row) if row else None

    def record_message(
        self,
        thread_key: str,
        msg_key: str,
        *,
        direction: str,
        text: str,
        author: str = "",
    ) -> bool:
        """Simpan pesan; True bila pesan ini baru."""
        with self.conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO chat_messages(thread_key, msg_key, direction, author, text, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (thread_key, msg_key, direction, author, text, utcnow_iso()),
            )
        return cursor.rowcount > 0

    def mark_answered(self, thread_key: str, msg_key: str, reply_text: str) -> None:
        now = utcnow_iso()
        with self.conn() as conn:
            conn.execute(
                "UPDATE chat_messages SET answered=1, reply_text=?, replied_at=? "
                "WHERE thread_key=? AND msg_key=?",
                (reply_text, now, thread_key, msg_key),
            )
            conn.execute(
                "UPDATE chats SET last_reply_at=?, last_reply_text=?, updated_at=? WHERE thread_key=?",
                (now, reply_text, now, thread_key),
            )

    def mark_answered_many(
        self,
        thread_key: str,
        msg_keys: list[str],
        reply_text: str,
        *,
        note: str = "dibalas",
    ) -> None:
        """Tandai pesan sudah ditangani.

        ``note`` membedakan "dibalas" (ada balasan nyata) dari "diam" (keputusan
        sengaja tidak menjawab). Pesan yang hanya diuji kering TIDAK boleh
        ditandai, supaya putaran berikutnya masih bisa mengirim.
        """
        if not msg_keys:
            return
        now = utcnow_iso()
        placeholders = ",".join("?" for _ in msg_keys)
        with self.conn() as conn:
            conn.execute(
                f"UPDATE chat_messages SET answered=1, reply_text=?, replied_at=?, "
                f"author=CASE WHEN author='' THEN ? ELSE author END "
                f"WHERE thread_key=? AND msg_key IN ({placeholders})",
                [reply_text, now, note, thread_key, *msg_keys],
            )
            conn.execute(
                "UPDATE chats SET last_checked_at=?, updated_at=? WHERE thread_key=?",
                (now, now, thread_key),
            )
            if reply_text and note == "dibalas":
                conn.execute(
                    "UPDATE chats SET last_reply_at=?, last_reply_text=?, updated_at=? "
                    "WHERE thread_key=?",
                    (now, reply_text, now, thread_key),
                )

    def fix_message_direction(self, thread_key: str, msg_key: str, *, direction: str) -> bool:
        """Perbaiki arah pesan bila pembacaan ulang menunjukkan hal berbeda."""
        if direction not in ("in", "out"):
            return False
        with self.conn() as conn:
            cursor = conn.execute(
                "UPDATE chat_messages SET direction=? "
                "WHERE thread_key=? AND msg_key=? AND direction<>?",
                (direction, thread_key, msg_key, direction),
            )
        return cursor.rowcount > 0

    def thread_history(self, thread_key: str, limit: int = 30) -> list[dict]:
        """Riwayat pesan satu chat (untuk menentukan mana yang sudah ditanggapi)."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT msg_key, direction, text, answered, reply_text, created_at "
                "FROM chat_messages WHERE thread_key=? ORDER BY id DESC LIMIT ?",
                (thread_key, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def replied_texts(self, thread_key: str) -> list[str]:
        """Teks balasan yang benar-benar pernah terkirim di chat ini."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT reply_text FROM chat_messages "
                "WHERE thread_key=? AND direction='in' AND reply_text<>'' ORDER BY id",
                (thread_key,),
            ).fetchall()
        return [row["reply_text"] for row in rows]

    def reopen_messages(self, thread_key: str, texts: set[str]) -> int:
        """Buka kembali pesan masuk yang tertandai terjawab padahal tak pernah dibalas.

        ``texts`` berisi cuplikan teks ternormalisasi (maks 60 karakter) yang
        ditandai Facebook sebagai belum dibaca. Hanya baris tanpa balasan yang
        disentuh, sehingga balasan yang benar-benar terkirim tidak terulang.
        """
        if not texts:
            return 0
        reopened = 0
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT id, text FROM chat_messages "
                "WHERE thread_key=? AND direction='in' AND answered=1 AND reply_text=''",
                (thread_key,),
            ).fetchall()
            for row in rows:
                normalized = " ".join(str(row["text"] or "").lower().split())[:60]
                if normalized in texts:
                    conn.execute(
                        "UPDATE chat_messages SET answered=0, replied_at=NULL WHERE id=?",
                        (row["id"],),
                    )
                    reopened += 1
        return reopened

    def reset_unanswered_marks(self) -> int:
        """Perbaiki tanda 'terjawab' yang dibuat tanpa balasan (bug uji kering).

        Hanya berlaku untuk baris tanpa balasan (``reply_text=''``), jadi pesan
        yang benar-benar dibalas tidak tersentuh. Idempoten.
        """
        with self.conn() as conn:
            cursor = conn.execute(
                "UPDATE chat_messages SET answered=0, replied_at=NULL "
                "WHERE answered=1 AND reply_text=''"
            )
            return int(cursor.rowcount or 0)

    def escalation_exists(
        self, chat_url: str, message: str, *, only_failed: bool = False
    ) -> bool:
        """Sudah pernah dieskalasi?

        ``only_failed`` membatasi ke eskalasi yang **gagal terkirim**: notifikasi
        yang tidak pernah sampai ke WhatsApp harus dicoba lagi, bukan dianggap
        "sudah pernah" lalu didiamkan selamanya.
        """
        query = "SELECT 1 FROM escalations WHERE chat_url=? AND message=?"
        params: list[Any] = [chat_url, message[:500]]
        if only_failed:
            query += " AND status='gagal'"
        with self.conn() as conn:
            row = conn.execute(query + " LIMIT 1", params).fetchone()
        return row is not None

    def find_escalation(self, chat_url: str, message: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM escalations WHERE chat_url=? AND message=? "
                "ORDER BY id DESC LIMIT 1",
                (chat_url, message[:500]),
            ).fetchone()
        return dict(row) if row else None

    def unanswered_incoming(self, thread_key: str) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE thread_key=? AND direction='in' "
                "AND answered=0 ORDER BY id",
                (thread_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def outgoing_texts(self, thread_key: str, limit: int = 20) -> list[str]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT text FROM chat_messages WHERE thread_key=? AND direction='out' "
                "ORDER BY id DESC LIMIT ?",
                (thread_key, max(1, min(limit, 100))),
            ).fetchall()
        return [row["text"] for row in rows]

    def last_outgoing(self, thread_key: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM chat_messages WHERE thread_key=? AND direction='out' "
                "ORDER BY id DESC LIMIT 1",
                (thread_key,),
            ).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- eskalasi
    def add_escalation(
        self,
        *,
        chat_name: str,
        message: str,
        reason: str,
        chat_url: str = "",
        channel: str = "whatsapp-web",
    ) -> int:
        with self.conn() as conn:
            cursor = conn.execute(
                "INSERT INTO escalations(created_at, chat_name, chat_url, message, reason, channel) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (utcnow_iso(), chat_name, chat_url, message[:500], reason, channel),
            )
            return int(cursor.lastrowid)

    def mark_escalation(
        self, escalation_id: int, status: str, *, error: str = "", channel: str = ""
    ) -> None:
        sent_at = utcnow_iso() if status == "terkirim" else None
        with self.conn() as conn:
            conn.execute(
                "UPDATE escalations SET status=?, error=?, attempts=attempts+1, "
                "channel=CASE WHEN ? != '' THEN ? ELSE channel END, "
                "sent_at=COALESCE(?, sent_at) WHERE id=?",
                (status, error, channel, channel, sent_at, int(escalation_id)),
            )

    def reset_escalation(self, escalation_id: int, *, channel: str = "") -> None:
        """Siapkan ulang eskalasi yang gagal untuk dicoba kirim lagi."""
        with self.conn() as conn:
            conn.execute(
                "UPDATE escalations SET status='antre', error='', "
                "channel=CASE WHEN ? != '' THEN ? ELSE channel END WHERE id=?",
                (channel, channel, int(escalation_id)),
            )

    def delete_escalation(self, esc_id: int) -> None:
        with self.conn() as conn:
            conn.execute("DELETE FROM escalations WHERE id=?", (esc_id,))

    def list_escalations(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM escalations"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_pending_escalations(self) -> int:
        with self.conn() as conn:
            return int(
                conn.execute("SELECT COUNT(*) c FROM escalations WHERE status='antre'").fetchone()["c"]
            )

    def chat_stats(self) -> dict[str, int]:
        with self.conn() as conn:
            chats = conn.execute("SELECT COUNT(*) c FROM chats").fetchone()["c"]
            messages = conn.execute("SELECT COUNT(*) c FROM chat_messages").fetchone()["c"]
            outgoing = conn.execute(
                "SELECT COUNT(*) c FROM chat_messages WHERE direction='out'"
            ).fetchone()["c"]
            pending = conn.execute(
                "SELECT COUNT(*) c FROM escalations WHERE status='antre'"
            ).fetchone()["c"]
        return {
            "chats": int(chats),
            "messages": int(messages),
            "outgoing": int(outgoing),
            "pending_escalations": int(pending),
        }

    def delete_chat_data(self) -> None:
        with self.conn() as conn:
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM chats")
            conn.execute("DELETE FROM escalations")

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        with self.conn() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
            by_source = {
                r["source"]: r["c"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) c FROM posts GROUP BY source"
                ).fetchall()
            }
            by_status = {
                r["workflow_status"]: r["c"]
                for r in conn.execute(
                    "SELECT workflow_status, COUNT(*) c FROM posts GROUP BY workflow_status"
                ).fetchall()
            }
            by_category = {
                r["category"]: r["c"]
                for r in conn.execute(
                    "SELECT category, COUNT(*) c FROM analyses GROUP BY category"
                ).fetchall()
            }
            analysed = conn.execute(
                "SELECT COUNT(*) c FROM posts WHERE analysis_status LIKE 'dianalisis%'"
            ).fetchone()["c"]
        return {
            "total": total,
            "by_source": by_source,
            "by_status": by_status,
            "by_category": by_category,
            "analysed": analysed,
            "db_path": str(self.path),
            "db_size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0.0,
        }

    def delete_jobs_and_events(self) -> None:
        with self.conn() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM jobs")


# --------------------------------------------------------------------- util
def _row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    details = data.get("details_json")
    reasons = data.get("reasons_json")
    try:
        data["details"] = json.loads(details) if details else {}
    except (TypeError, json.JSONDecodeError):
        data["details"] = {}
    try:
        data["reasons"] = json.loads(reasons) if reasons else []
    except (TypeError, json.JSONDecodeError):
        data["reasons"] = []
    try:
        data["filter_reasons"] = json.loads(data.get("filter_reasons_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        data["filter_reasons"] = []
    return data


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key, default in (
        ("group_urls_json", []),
        ("config_json", {}),
        ("counters_json", {}),
        ("diagnostics_json", {}),
    ):
        try:
            data[key.replace("_json", "")] = json.loads(data.get(key) or json.dumps(default))
        except (TypeError, json.JSONDecodeError):
            data[key.replace("_json", "")] = default
    return data


def get_db(path: str | Path) -> Database:
    return Database(path)


def human_time(iso: str | None) -> str:
    """Tampilkan waktu ISO dalam waktu lokal, atau penanda tidak diketahui."""
    if not iso:
        return UNKNOWN
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return UNKNOWN
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d %b %Y %H:%M")
