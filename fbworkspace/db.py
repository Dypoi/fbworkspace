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

SCHEMA_VERSION = "1"

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
    heartbeat_at   TEXT
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
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )

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
            "started_at", "finished_at", "heartbeat_at",
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
    for key, default in (("group_urls_json", []), ("config_json", {}), ("counters_json", {})):
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
