"""Fixture bersama untuk pengujian."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fbworkspace.db import Database  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture()
def sample_texts() -> dict[str, str]:
    return {
        "selling": (
            "Dijual iPhone 13 Pro Max 256GB iBox fullset, garansi habis, BH 89%, "
            "mulus no minus. Harga Rp 12.500.000 nego. COD Tangerang Selatan."
        ),
        "buying": (
            "Cari iPhone 11 128gb budget 4jt, kondisi normal. COD Jakarta Selatan. DM ya."
        ),
        "discussion": (
            "Menurut kalian iPhone 15 Pro vs 14 Pro Max lebih worth it buat foto?"
        ),
        "irrelevant": "Jual kulkas 2 pintu murah, COD Depok. Harga nego.",
    }
