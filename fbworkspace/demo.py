"""Pemuatan data DEMO (sintetis) untuk mencoba aplikasi tanpa Facebook.

Semua postingan di data demo diberi label [DEMO] dan tautan grup palsu.
Tidak ada data nyata dari Facebook.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import PROJECT_ROOT
from .db import Database
from .models import FilterConfig, Source
from .pipeline import ingest_post

DEMO_PATH = PROJECT_ROOT / "data" / "demo_posts_DEMO.json"


def demo_available(path: Path = DEMO_PATH) -> bool:
    return path.exists()


def load_demo_posts(
    db: Database,
    filter_config: FilterConfig | None = None,
    *,
    path: Path = DEMO_PATH,
    strict_unknown: bool = False,
) -> dict:
    """Muat data demo ke database. Aman dijalankan berulang (dedupe otomatis)."""
    if not path.exists():
        return {"ok": False, "message": f"File demo tidak ditemukan: {path}", "inserted": 0}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"File demo tidak valid: {exc}", "inserted": 0}

    posts = payload.get("posts") or []
    inserted = 0
    duplicates = 0
    errors = 0
    for item in posts:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        outcome = ingest_post(
            db,
            text=text,
            source=Source.DEMO.value,
            group_url=item.get("group_url") or "https://www.facebook.com/groups/DEMO/",
            post_url=item.get("post_url"),
            posted_at_raw=item.get("posted_at_raw"),
            filter_config=filter_config or FilterConfig(),
            strict_unknown=strict_unknown,
        )
        if outcome.error:
            errors += 1
        elif outcome.is_new:
            inserted += 1
        else:
            duplicates += 1

    return {
        "ok": True,
        "message": (
            f"Data demo dimuat: {inserted} baru, {duplicates} sudah ada, {errors} gagal. "
            "Semua data demo bersifat sintetis."
        ),
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "total_in_file": len(posts),
    }
