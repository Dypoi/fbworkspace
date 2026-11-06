"""Ekspor CSV yang aman (anti formula injection).

Masalah yang dicegah: Excel/Sheets mengeksekusi sel yang diawali
= + - @ atau karakter kontrol sebagai formula. Karena teks postingan berasal
dari internet (data tidak tepercaya), setiap sel dibersihkan lebih dulu.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Iterable

# Karakter yang bisa memicu formula di Excel/LibreOffice/Sheets
_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_FORMULA_TRIGGERS = ("=", "+", "-", "@")


def sanitize_cell(value: Any) -> str:
    """Ubah satu nilai menjadi teks yang aman untuk CSV."""
    if value is None:
        return ""
    text = str(value)
    # buang NUL & karakter kontrol yang bisa merusak file
    text = text.replace("\x00", "").replace("\r", "\n")
    stripped = text.lstrip()
    if text.startswith(_DANGEROUS_PREFIXES):
        return "'" + text
    # spasi di depan lalu "=" juga dieksekusi Excel -> amankan
    if stripped.startswith(_FORMULA_TRIGGERS) and stripped != text:
        return "'" + text
    if len(text) > 5000:  # jaga ukuran file
        text = text[:4997] + "…"
    return text


def _format_amount(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return sanitize_cell(value)


def _format_details(details: Any) -> str:
    if not isinstance(details, dict) or not details:
        return ""
    return "; ".join(f"{k}: {v}" for k, v in details.items())


def _format_reasons(reasons: Any) -> str:
    if isinstance(reasons, list):
        return " | ".join(str(r) for r in reasons)
    if reasons:
        return str(reasons)
    return ""


EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("group_url", "Tautan Grup"),
    ("post_url", "Tautan Postingan"),
    ("text", "Teks Postingan"),
    ("found_at", "Waktu Ditemukan (UTC)"),
    ("posted_at", "Waktu Posting (perkiraan)"),
    ("posted_at_precision", "Presisi Waktu"),
    ("source", "Sumber Data"),
    ("workflow_status", "Status"),
    ("category", "Kategori"),
    ("iphone_model", "Model iPhone"),
    ("price_amount", "Nominal"),
    ("price_kind", "Jenis Nominal"),
    ("location", "Lokasi"),
    ("details", "Detail Disebutkan"),
    ("relevance_score", "Skor Relevansi"),
    ("reasons", "Alasan"),
    ("snippet", "Cuplikan"),
)


def _value_for(row: dict, key: str) -> str:
    value = row.get(key)
    if key in {"price_amount"}:
        return _format_amount(value)
    if key == "details":
        # Selalu sanitasi SETELAH diformat: nilai gabungan juga bisa diawali '='.
        return sanitize_cell(_format_details(value))
    if key == "reasons":
        return sanitize_cell(_format_reasons(value))
    if isinstance(value, (list, dict)):
        return sanitize_cell(str(value))
    return sanitize_cell(value)


def rows_to_csv(rows: Iterable[dict]) -> str:
    """Bangun isi CSV (string) dari daftar postingan."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([label for _key, label in EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([_value_for(row, key) for key, _label in EXPORT_COLUMNS])
    return buffer.getvalue()


def rows_to_csv_bytes(rows: Iterable[dict]) -> bytes:
    """CSV sebagai bytes UTF-8 dengan BOM agar Excel Windows membaca huruf dengan benar."""
    return b"\xef\xbb\xbf" + rows_to_csv(rows).encode("utf-8")


def suggested_filename(prefix: str = "fbworkspace") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}.csv"
