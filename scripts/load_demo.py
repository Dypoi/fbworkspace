"""Muat data DEMO ke database dari command line (tanpa dashboard).

    python scripts/load_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fbworkspace.config import get_settings  # noqa: E402
from fbworkspace.db import Database  # noqa: E402
from fbworkspace.demo import load_demo_posts  # noqa: E402


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    result = load_demo_posts(db, None)
    status = "OK" if result.get("ok") else "GAGAL"
    print(f"[{status}] {result.get('message')}")
    print(f"Database: {settings.db_path}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
