"""Filter kata kunci, lokasi, dan rentang harga.

Pencocokan kata kunci memakai batas kata (word boundary) supaya "ip" tidak
mencocokkan "tips". Frasa multi-kata dicocokkan apa adanya (subset).

Keterbatasan yang perlu diketahui:
- Hanya bekerja pada teks postingan, bukan gambar/komentar.
- Bahasa gaul/singkatan yang tidak ada di daftar tidak akan dikenali.
- "Harga tidak diketahui" bisa berarti benar-benar tidak ada atau formatnya
  tidak dikenali. Secara default postingan seperti itu tetap lolos dengan
  catatan, kecuali Anda menyalakan mode ketat.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .analyzer import analyze_text, extract_location
from .models import UNKNOWN, FilterConfig, FilterResult
from .normalize import normalize_text

_SPLIT_RE = re.compile(r"[\n,;]+")


def parse_keywords(raw: str | list[str] | None) -> list[str]:
    """Pecah input pengguna menjadi daftar kata kunci unik (urutan dipertahankan)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = _SPLIT_RE.split(str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        kw = item.strip()
        if not kw:
            continue
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(kw)
    return out


def match_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    pattern = re.escape(keyword.strip())
    # Frasa (mengandung spasi) -> substring; kata tunggal -> batas kata.
    if " " in keyword.strip():
        return re.search(pattern, text, re.I) is not None
    return re.search(rf"(?<!\w){pattern}(?!\w)", text, re.I) is not None


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if match_keyword(text, kw)]


def evaluate(
    text: str,
    config: FilterConfig,
    *,
    analysis: dict | None = None,
    strict_unknown: bool = False,
) -> FilterResult:
    """Tentukan apakah postingan lolos filter.

    `analysis` opsional (hasil analyze_text) agar tidak dihitung dua kali.
    """
    clean = normalize_text(text)
    if not clean:
        return FilterResult(False, ["Teks kosong."])

    result = analysis or analyze_text(clean).as_dict()
    reasons: list[str] = []
    missing: list[str] = []

    # 1) iPhone wajib
    category = result.get("category", UNKNOWN)
    mentions_iphone = bool(re.search(r"\b(iphone|ip\s?\d{1,2})", clean, re.I))
    if config.require_iphone and not mentions_iphone:
        return FilterResult(
            False,
            ["Tidak menyebut iPhone."],
            ["iphone"],
        )
    if mentions_iphone:
        reasons.append("Menyebut iPhone.")

    # 2) Kata kunci eksklusi (dicek lebih dulu: kalau kena, langsung gugur)
    excluded = find_keywords(clean, config.exclude_keywords)
    if excluded:
        return FilterResult(
            False,
            [f"Mengandung kata kunci eksklusi: {', '.join(excluded)}"],
            [],
        )

    # 3) Kata kunci inklusi
    if config.include_keywords:
        included = find_keywords(clean, config.include_keywords)
        if not included:
            return FilterResult(
                False,
                ["Tidak mengandung kata kunci inklusi apa pun."],
                ["kata kunci inklusi"],
            )
        reasons.append(f"Kata kunci cocok: {', '.join(included)}.")

    # 4) Lokasi
    if config.locations:
        location = result.get("location") or extract_location(clean)
        if location == UNKNOWN:
            missing.append("lokasi")
            if strict_unknown:
                return FilterResult(
                    False,
                    reasons + ["Lokasi tidak diketahui (mode ketat)."],
                    missing,
                )
            reasons.append("Lokasi tidak diketahui: tetap lolos (mode longgar).")
        elif any(loc.lower() in location.lower() or location.lower() in loc.lower()
                 for loc in config.locations):
            reasons.append(f"Lokasi cocok: {location}.")
        else:
            return FilterResult(
                False,
                [f"Lokasi {location} tidak termasuk pilihan."],
                [],
            )

    # 5) Rentang harga
    if config.price_min is not None or config.price_max is not None:
        amount = result.get("price_amount")
        if amount is None:
            missing.append("harga")
            if strict_unknown:
                return FilterResult(
                    False,
                    reasons + ["Harga tidak diketahui (mode ketat)."],
                    missing,
                )
            reasons.append("Harga tidak diketahui: tetap lolos (mode longgar).")
        else:
            if config.price_min is not None and amount < config.price_min:
                return FilterResult(
                    False,
                    [f"Harga Rp{amount:,} di bawah batas minimal Rp{config.price_min:,}."],
                    [],
                )
            if config.price_max is not None and amount > config.price_max:
                return FilterResult(
                    False,
                    [f"Harga Rp{amount:,} di atas batas maksimal Rp{config.price_max:,}."],
                    [],
                )
            reasons.append(f"Harga Rp{amount:,} dalam rentang.")

    # 6) Kategori tidak relevan
    if category == "tidak relevan":
        return FilterResult(False, reasons + ["Kategori: tidak relevan."], missing)

    return FilterResult(True, reasons or ["Lolos tanpa filter khusus."], missing)


def config_from_ui(
    *,
    include_raw: str,
    exclude_raw: str,
    locations_raw: str,
    price_min: int | None,
    price_max: int | None,
    require_iphone: bool = True,
) -> FilterConfig:
    cfg = FilterConfig(
        include_keywords=parse_keywords(include_raw),
        exclude_keywords=parse_keywords(exclude_raw),
        locations=parse_keywords(locations_raw),
        price_min=price_min,
        price_max=price_max,
        require_iphone=require_iphone,
    )
    return cfg


def config_to_json(config: FilterConfig) -> dict:
    return {
        "include_keywords": config.include_keywords,
        "exclude_keywords": config.exclude_keywords,
        "locations": config.locations,
        "price_min": config.price_min,
        "price_max": config.price_max,
        "require_iphone": config.require_iphone,
    }


def config_from_json(data: dict) -> FilterConfig:
    return FilterConfig(
        include_keywords=list(data.get("include_keywords") or []),
        exclude_keywords=list(data.get("exclude_keywords") or []),
        locations=list(data.get("locations") or []),
        price_min=data.get("price_min"),
        price_max=data.get("price_max"),
        require_iphone=bool(data.get("require_iphone", True)),
    )


def with_defaults(config: FilterConfig, **overrides) -> FilterConfig:
    return replace(config, **overrides)
