"""Daftar harga & stok untuk membalas chat DM.

Prinsip:
- Harga HANYA boleh berasal dari daftar ini. Kalau model/storage tidak ada,
  worker tidak menebak — pertanyaannya dieskalasi ke Anda lewat WhatsApp.
- Nomor harga disimpan sebagai integer rupiah, bukan string, supaya tidak ada
  salah baca "1.700.000" vs "1,7".
- Daftar bawaan di bawah ini diambil dari pricelist yang Anda kirim
  (6 November). Anda bisa mengubahnya dari tab **Balas**.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import collapse_ws

# --------------------------------------------------------------------------
# Daftar bawaan (boleh diubah lewat UI; ini hanya nilai awal)
# --------------------------------------------------------------------------

DEFAULT_PRICES: tuple[tuple[str, str, int | None, int], ...] = (
    ("iphone se gen2", "iPhone SE (2020)", 64, 1_700_000),
    ("iphone se gen2", "iPhone SE (2020)", 128, 2_000_000),
    ("iphone 8", "iPhone 8", 64, 1_100_000),
    ("iphone 8", "iPhone 8", 256, 1_300_000),
    ("iphone xr", "iPhone XR", 64, 2_100_000),
    ("iphone xr", "iPhone XR", 128, 2_300_000),
    ("iphone 11", "iPhone 11", 64, 2_400_000),
    ("iphone 11", "iPhone 11", 128, 2_600_000),
    ("iphone 11", "iPhone 11", 256, 2_800_000),
)

_STORAGE_OPTIONS = (16, 32, 64, 128, 256, 512, 1024)


@dataclass(frozen=True)
class PriceEntry:
    """Satu baris harga: satu model, satu storage."""

    model_key: str
    label: str
    storage: int | None
    price: int
    ready: bool = True

    @property
    def price_text(self) -> str:
        return format_rupiah(self.price)

    def as_dict(self) -> dict:
        return {
            "model_key": self.model_key,
            "label": self.label,
            "storage": self.storage,
            "price": self.price,
            "ready": self.ready,
        }


def default_entries() -> list[PriceEntry]:
    return [
        PriceEntry(model_key=key, label=label, storage=storage, price=price)
        for key, label, storage, price in DEFAULT_PRICES
    ]


# --------------------------------------------------------------------------
# Format & parsing
# --------------------------------------------------------------------------


def format_rupiah(amount: int | None) -> str:
    """1700000 -> '1.700.000' (gaya Indonesia)."""
    if amount is None:
        return ""
    return f"{int(amount):,}".replace(",", ".")


def canonical_key(text: str) -> str:
    """Bentuk baku nama model: 'iPhone 11' -> 'iphone 11'."""
    value = collapse_ws((text or "").lower())
    value = value.replace("i phone", "iphone")
    value = re.sub(r"\biphone\s*se\s*(?:gen\s*2|gen2|2020|2)\b", "iphone se gen2", value)
    value = re.sub(r"(?<!iphone )\bse\s*(?:gen\s*2|gen2|2020)\b", "iphone se gen2", value)
    value = re.sub(r"\biphone(?:\s+iphone)+", "iphone", value)
    value = re.sub(r"\s+", " ", value).strip()
    if value and not value.startswith("iphone"):
        value = f"iphone {value}"
    return value


def label_for(key: str) -> str:
    """'iphone se gen2' -> 'iPhone SE (2020)'."""
    value = (key or "").lower().replace("iphone", "").strip()
    if not value:
        return "iPhone"
    if value.startswith("se"):
        return "iPhone SE (2020)"
    parts = value.split()
    pretty = []
    for part in parts:
        if part in {"xr", "xs"}:
            pretty.append(part.upper())
        elif part in {"pro", "max", "plus", "mini"}:
            pretty.append(part.title())
        else:
            pretty.append(part)
    return "iPhone " + " ".join(pretty)


def parse_price_line(line: str) -> PriceEntry | None:
    """Baca satu baris pricelist, mis.:

    '- iphone 8 64gb: 1.100.000'
    'iPhone Xr 128 GB - 2.300.000'
    'iphone 11 256gb 2,8jt'

    Mengembalikan None bila harga/model tidak bisa dipastikan.
    """
    raw = collapse_ws(line)
    if not raw:
        return None
    raw = re.sub(r"^[\s\-•*⁠]+", "", raw)
    raw = re.sub(r"^[-•*⁠]+", "", raw).strip()
    if not raw:
        return None

    price, rest = _split_price(raw)
    if price is None:
        return None

    storage = _first_storage(rest)
    model_part = _strip_storage(rest)
    model_part = re.sub(r"[\s:=\-|]+$", "", model_part).strip()
    model_part = re.sub(r"\b(harga|price|rp)\b", "", model_part, flags=re.I).strip()
    model_part = collapse_ws(model_part.replace(":", " "))
    if not model_part:
        return None
    key = canonical_key(model_part)
    return PriceEntry(model_key=key, label=label_for(key), storage=storage, price=price)


def _split_price(raw: str) -> tuple[int | None, str]:
    """Pisahkan bagian harga dari bagian model. ('1.100.000', 'iphone 8 64gb')."""
    # Bentuk dengan satuan: "2,8jt", "1,7 juta", "1700 rb"
    match = re.search(
        r"(?:rp\.?\s*)?(\d+(?:[.,]\d+)?)\s*(jt|juta)\b", raw, re.IGNORECASE
    )
    if match:
        amount = float(match.group(1).replace(",", ".")) * 1_000_000
        return int(round(amount)), _remove(raw, match)

    match = re.search(
        r"(?:rp\.?\s*)?(\d+(?:[.,]\d+)?)\s*(rb|ribu)\b", raw, re.IGNORECASE
    )
    if match:
        amount = float(match.group(1).replace(",", ".")) * 1_000
        return int(round(amount)), _remove(raw, match)

    # Bentuk angka penuh dengan titik ribuan: 1.100.000
    for match in re.finditer(r"\d[\d.]{5,}", raw):
        digits = match.group(0).replace(".", "")
        if digits.isdigit() and len(digits) >= 6:
            return int(digits), _remove(raw, match)

    return None, raw


def _remove(text: str, match: re.Match[str]) -> str:
    return (text[: match.start()] + " " + text[match.end() :]).strip()


def _first_storage(text: str) -> int | None:
    match = re.search(r"\b(\d{2,4})\s*(?:gb|g)\b", text, re.IGNORECASE)
    if match:
        value = int(match.group(1))
        if value in _STORAGE_OPTIONS:
            return value
    return None


def _strip_storage(text: str) -> str:
    return re.sub(r"\b\d{2,4}\s*(?:gb|g)\b", " ", text, flags=re.IGNORECASE)


# --------------------------------------------------------------------------
# Pencarian
# --------------------------------------------------------------------------


def find_entries(entries: list[PriceEntry], model_key: str) -> list[PriceEntry]:
    return [entry for entry in entries if entry.model_key == model_key]


def storages_for(entries: list[PriceEntry], model_key: str) -> list[int]:
    return sorted({entry.storage for entry in find_entries(entries, model_key) if entry.storage})


def find_exact(
    entries: list[PriceEntry], model_key: str, storage: int | None
) -> PriceEntry | None:
    for entry in find_entries(entries, model_key):
        if entry.storage == storage:
            return entry
    return None


def is_ready(entries: list[PriceEntry], model_key: str, storage: int | None = None) -> bool:
    candidates = find_entries(entries, model_key)
    if storage is not None:
        narrowed = [entry for entry in candidates if entry.storage in (None, storage)]
        candidates = narrowed or candidates
    return any(entry.ready for entry in candidates)
