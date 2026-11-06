"""Normalisasi teks, tautan, harga, dan waktu.

Semua fungsi di sini murni (tanpa I/O) sehingga mudah diuji.
Prinsip: **jangan menebak**. Kalau tidak yakin, kembalikan None / "tidak diketahui".
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlparse

from .models import UNKNOWN, PriceKind

# --------------------------------------------------------------------------
# Teks
# --------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_QUOTE_MAP = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00ab": '"',
    "\u00bb": '"',
}
_DASH_MAP = {
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
}


def normalize_text(value: str | None) -> str:
    """Rapikan teks: Unicode NFKC, buang zero-width, samakan tanda kutip, rapikan spasi."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.translate(_ZERO_WIDTH)
    for src, dst in {**_QUOTE_MAP, **_DASH_MAP}.items():
        text = text.replace(src, dst)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    # buang baris kosong berlebih
    cleaned: list[str] = []
    for line in lines:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


# --------------------------------------------------------------------------
# Kualitas teks hasil pembacaan browser
# --------------------------------------------------------------------------

# Facebook menyisipkan karakter format tak terlihat (mis. U+034F) di antara
# huruf, dan label "Facebook" berulang, untuk mengganggu pembacaan otomatis.
_INVISIBLE_CHARS = dict.fromkeys(
    map(
        ord,
        "\u034f\u061c\u180e\u200b\u200c\u200d\u200e\u200f\u2060\ufeff",
    ),
    None,
)
_FACEBOOK_LABEL_LINE = re.compile(r"(?i)^(?:\s*facebook\s*[\u00b7|]?\s*)+$")


def strip_invisible_noise(value: str) -> str:
    """Buang karakter tak terlihat dan baris teks pengganggu ala Facebook.

    Yang dibuang:
    - karakter format tak terlihat (CGJ U+034F, zero-width, dll),
    - baris yang hanya berisi label "Facebook" berulang,
    - baris "huruf berjauhan" (>=60% token satu huruf) yang biasanya hasil
      penyisipan karakter tak terlihat.

    Tidak mengubah kata normal: kalimat biasa tetap utuh.
    """
    text = (value or "").translate(_INVISIBLE_CHARS)
    lines: list[str] = []
    for line in text.split("\n"):
        if _FACEBOOK_LABEL_LINE.match(line):
            continue
        tokens = line.split()
        if len(tokens) >= 6:
            short = sum(1 for token in tokens if len(re.sub(r"\W", "", token)) <= 1)
            if short / len(tokens) >= 0.6:
                continue
        lines.append(line)
    return "\n".join(lines)

# Komentar Facebook selalu diakhiri tombol aksi "Reply"/"Balas" (kadang diikuti
# angka balasan), atau memuat rangkaian "LikeReplyShare" yang menempel.
_COMMENT_TRAILING = re.compile(
    r"(?i)\b(?:reply|balas)\s*\d*\s*"
    r"(?:(?:see|lihat)\s+(?:translation|terjemahan))?\s*(?:edited|diedit)?\s*$"
)
# Di UI Facebook, tombol aksi sering menempel ("LikeReplyShare",
# "LikeReplySee translationShare") atau terpisah ("Like Reply Share").
# Rangkaian tiga tombol ini tidak muncul di isi postingan biasa.
_COMMENT_ACTIONS = re.compile(
    r"(?i)(?:like|suka)(?:reply|balas)"
    r"|(?:reply|balas)(?:share|bagikan)"
    r"|(?:like|suka)\s*[\u00b7|]?\s*(?:reply|balas)\s*[\u00b7|]?\s*(?:share|bagikan)"
    r"|(?:reply|balas)\s*[\u00b7|]?\s*(?:share|bagikan)"
)
_TRANSLATION = re.compile(r"(?i)\b(?:see translation|lihat terjemahan)\b")


def looks_like_comment(text: str) -> bool:
    """Tebak apakah teks ini komentar/reply, bukan isi postingan.

    Dipakai dua tempat: adapter browser (agar komentar tidak disimpan) dan
    pembersih data lama. Sengaja konservatif: hanya menandai pola yang sangat
    khas komentar, supaya postingan pendek yang sah tidak ikut terbuang.
    """
    clean = collapse_ws(text)
    if not clean:
        return False
    if _COMMENT_TRAILING.search(clean):
        return True
    if _COMMENT_ACTIONS.search(clean):
        return True
    return bool(len(clean) <= 120 and _TRANSLATION.search(clean))


def text_hash(value: str) -> str:
    """Hash teks yang dinormalisasi (lowercase + spasi dirapatkan).

    Dipakai HANYA sebagai fallback deduplikasi ketika tautan/ID postingan tidak ada.
    Keterbatasan: dua postingan berbeda dengan teks identik akan dianggap sama.
    """
    base = collapse_ws(normalize_text(value)).lower()
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------
# Tautan Facebook
# --------------------------------------------------------------------------

_FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "mbasic.facebook.com",
    "fb.com",
    "www.fb.com",
}
_GROUP_PATH_RE = re.compile(r"^/groups/([^/?#]+)(?:/.*)?$")
_GROUP_ID_PATH_RE = re.compile(r"^/groups/(\d+)/?$")


@dataclass
class GroupUrlValidation:
    ok: bool
    normalized: str = ""
    group_id: str = ""
    message: str = ""
    warning: str = ""


def validate_group_url(raw: str) -> GroupUrlValidation:
    """Validasi tautan grup Facebook.

    Yang diterima: http(s)://(www|m|web).facebook.com/groups/<slug-atau-id>/
    Yang ditolak: domain lain, tautan postingan/profil, halaman tanpa /groups/.

    Catatan jujur: tautan yang valid TIDAK menjamin isi grup bisa dibaca.
    Grup publik pun bisa memerlukan login atau membatasi tampilan.
    """
    raw = (raw or "").strip()
    if not raw:
        return GroupUrlValidation(False, message="Tautan kosong.")
    if len(raw) > 500:
        return GroupUrlValidation(False, message="Tautan terlalu panjang.")

    candidate = raw if re.match(r"^https?://", raw, re.I) else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return GroupUrlValidation(False, message="Tautan tidak bisa dibaca.")

    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in _FB_HOSTS:
        return GroupUrlValidation(
            False,
            message=f"Bukan domain Facebook yang didukung: {host or '(kosong)'}.",
        )

    path = parsed.path or "/"
    share_match = re.match(r"^/share/(g|group)/([^/?#]+)", path)
    if share_match:
        normalized = f"https://www.facebook.com/share/g/{share_match.group(2)}/"
        return GroupUrlValidation(
            ok=True,
            normalized=normalized,
            group_id=share_match.group(2),
            message="Tautan berbagi grup dikenali.",
            warning=(
                "Tautan 'share' biasanya mengarah ke halaman perantara. "
                "Buka sekali di browser, lalu salin tautan grup aslinya (/groups/...) "
                "agar hasil pembacaan lebih dapat diandalkan."
            ),
        )

    match = _GROUP_PATH_RE.match(path)
    if not match:
        return GroupUrlValidation(
            False,
            message=(
                "Tautan harus mengarah ke grup, contoh: "
                "https://www.facebook.com/groups/123456789/"
            ),
        )

    group_id = match.group(1)
    normalized = f"https://www.facebook.com/groups/{group_id}/"
    warning = ""
    if not _GROUP_ID_PATH_RE.match(path):
        warning = (
            "Slug grup (bukan angka) bisa berubah dan tidak selalu bisa dipakai langsung. "
            "Kalau gagal, pakai tautan grup dengan ID angka."
        )
    return GroupUrlValidation(
        ok=True,
        normalized=normalized,
        group_id=group_id,
        message="Tautan grup terlihat valid.",
        warning=warning,
    )


def parse_group_urls(raw_block: str) -> list[GroupUrlValidation]:
    """Parse banyak tautan (dipisah baris / spasi / koma)."""
    parts = re.split(r"[\s,;]+", raw_block or "")
    results: list[GroupUrlValidation] = []
    seen: set[str] = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        result = validate_group_url(part)
        key = result.normalized or part
        if key in seen:
            continue
        seen.add(key)
        results.append(result)
    return results


def canonical_post_url(url: str | None) -> str:
    """Buang parameter pelacakan pada tautan postingan (untuk dedupe & tampilan)."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    host = (parsed.netloc or "").lower()
    if host not in _FB_HOSTS:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    # Permalink gaya lama memakai query (?story_fbid=...&id=...), jadi
    # untuk bentuk itu query yang relevan dipertahankan.
    if path.endswith("/permalink.php") or path.endswith("/story.php"):
        ids = extract_post_ids_from_url(url)
        if ids:
            return f"https://www.facebook.com{path}?story_fbid={ids[0]}"
    return f"https://www.facebook.com{path}"


_GROUP_ID_RE = re.compile(r"/groups/(\d+)")
_SET_POST_ID_RE = re.compile(r"(?:pcb|gm)\.(\d{5,})", re.I)


def group_id_from_url(url: str | None) -> str | None:
    """Ambil ID numerik grup dari tautan, mis. /groups/621052260194574/."""
    if not url:
        return None
    match = _GROUP_ID_RE.search(url)
    return match.group(1) if match else None


def post_id_from_set_param(url: str | None) -> str | None:
    """Ambil ID postingan dari parameter `set=pcb.<id>` atau `set=gm.<id>`.

    Tautan foto di grup memuat set tersebut; nilainya adalah ID postingan induk.
    """
    if not url:
        return None
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    for value in query.get("set", []):
        match = _SET_POST_ID_RE.search(value or "")
        if match:
            return match.group(1)
    return None


def build_group_post_url(group_url: str | None, post_id: str | None) -> str | None:
    """Susun tautan postingan grup dari ID grup + ID postingan."""
    group_id = group_id_from_url(group_url)
    if not group_id or not post_id:
        return None
    return f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"


def resolve_post_url(hrefs: list[str] | None, group_url: str | None) -> str | None:
    """Pilih tautan postingan terbaik dari tautan yang terlihat di satu kartu.

    Urutan: tautan postingan langsung -> turunkan dari tautan komentar
    (buang `comment_id`) -> turunkan dari `set=pcb./gm.` pada tautan foto.
    Kembalikan None bila memang tidak ada — jangan mengarang.
    """
    links = [href for href in (hrefs or []) if href]
    for href in links:
        if "comment_id=" in href:
            continue
        if "/posts/" in href or "/permalink/" in href or "story_fbid=" in href:
            canonical = canonical_post_url(href)
            if canonical and "/photo" not in canonical:
                return canonical
    for href in links:
        if "comment_id=" in href:
            canonical = canonical_post_url(href)
            if canonical and "/posts/" in canonical:
                return canonical
    for href in links:
        post_id = post_id_from_set_param(href)
        if post_id:
            resolved = build_group_post_url(group_url, post_id)
            if resolved:
                return resolved
    return None


def group_search_url(
    group_url: str | None, text: str, *, max_chars: int = 80
) -> str | None:
    """Tautan pencarian di dalam grup, untuk postingan tanpa permalink."""
    group_id = group_id_from_url(group_url)
    snippet = collapse_ws(text)[:max_chars]
    if not group_id or not snippet:
        return None
    return f"https://www.facebook.com/groups/{group_id}/search/?q={quote(snippet)}"


def extract_post_ids_from_url(url: str | None) -> list[str]:
    """Ambil kandidat ID postingan dari beberapa bentuk tautan Facebook."""
    if not url:
        return []
    ids: list[str] = []
    try:
        parsed = urlparse(url if re.match(r"^https?://", url) else f"https://{url}")
    except ValueError:
        return []
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")

    for pattern in (
        r"/posts/(\d+)",
        r"/permalink/(\d+)",
        r"/permalink\.php",
        r"/story\.php",
    ):
        m = re.search(pattern, path)
        if m and m.groups():
            ids.append(m.group(1))
    for key in ("story_fbid", "fbid", "id"):
        for value in query.get(key, []):
            if value.isdigit():
                ids.append(value)
    # buang duplikat, pertahankan urutan
    out: list[str] = []
    for item in ids:
        if item not in out:
            out.append(item)
    return out


def derive_dedupe_key(post_url: str | None, text: str) -> tuple[str, str]:
    """Tentukan kunci deduplikasi.

    Prioritas:
      1. ID postingan dari tautan  -> "urlid:<id>"  (paling kuat)
      2. Hash teks ternormalisasi  -> "texthash:<sha256>"  (fallback)
    Mengembalikan (key, method).
    """
    ids = extract_post_ids_from_url(post_url)
    if ids:
        return f"urlid:{ids[0]}", "url_id"
    return f"texthash:{text_hash(text)}", "text_hash"


# --------------------------------------------------------------------------
# Harga (format Indonesia)
# --------------------------------------------------------------------------

_PRICE_CONTEXT = re.compile(r"(rp|idr|harga|budget|bujet|bujet|mahal|murah|jt|juta|ribu|rb)", re.I)
_NUMBER = r"\d[\d.,]*"
_MULTIPLIER_MAP = {
    "jt": 1_000_000,
    "juta": 1_000_000,
    "jutaan": 1_000_000,
    "miliar": 1_000_000_000,
    "rb": 1_000,
    "ribu": 1_000,
}


@dataclass
class PriceMatch:
    amount: int
    raw: str
    start: int
    end: int


def _parse_number(raw: str) -> float | None:
    """Tafsirkan angka gaya Indonesia. Kembalikan None bila ambigu.

    Contoh: "12.500.000"->12500000, "12,5"->12.5, "12.500"->12500, "1.234,56"->1234.56
    Konvensi yang dipakai: titik = pemisah ribuan, koma = desimal.
    Bila sebuah angka benar-benar ambigu, fungsi mengembalikan None (lebih baik
    "tidak diketahui" daripada salah).
    """
    raw = (raw or "").strip().replace(" ", "")
    if not re.search(r"\d", raw):
        return None
    has_dot, has_comma = "." in raw, "," in raw

    if has_dot and has_comma:
        if raw.rfind(".") > raw.rfind(","):  # 1,234.56
            return _to_float(raw.replace(",", ""))
        return _to_float(raw.replace(".", "").replace(",", "."))  # 1.234,56
    if has_dot:
        parts = raw.split(".")
        if len(parts) > 2:
            return _to_float(raw.replace(".", "")) if all(len(p) == 3 for p in parts[1:]) else None
        left, right = parts
        if len(right) == 3 and left.isdigit():
            return _to_float(raw.replace(".", ""))
        if 1 <= len(right) <= 2:
            return _to_float(raw)
        return None
    if has_comma:
        parts = raw.split(",")
        if len(parts) > 2:
            return None
        left, right = parts
        if len(right) == 3 and len(left) <= 3 and left.isdigit():
            return _to_float(raw.replace(",", ""))
        if 1 <= len(right) <= 2:
            return _to_float(raw.replace(",", "."))
        return None
    return _to_float(raw)


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def find_prices(text: str, min_amount: int = 10_000, max_amount: int = 500_000_000) -> list[PriceMatch]:
    """Cari kandidat harga di teks.

    Hanya mengembalikan angka yang (a) jelas bentuknya dan (b) dalam rentang wajar.
    Angka tanpa konteks harga (mis. "iPhone 11") tidak dianggap harga.
    """
    if not text:
        return []
    results: list[PriceMatch] = []

    # 1) Rp / IDR diikuti angka
    for m in re.finditer(rf"(?:rp\.?|idr)\s*({_NUMBER})", text, re.I):
        amount = _parse_number(m.group(1))
        if amount is None:
            continue
        results.append(PriceMatch(int(amount), m.group(0), m.start(), m.end()))

    # 2) angka + multiplier (12,5jt / 850rb / 8 juta)
    for m in re.finditer(rf"\b({_NUMBER})\s*(jt|juta|jutaan|miliar|rb|ribu)\b", text, re.I):
        base = _parse_number(m.group(1))
        if base is None:
            continue
        amount = int(base * _MULTIPLIER_MAP[m.group(2).lower()])
        results.append(PriceMatch(amount, m.group(0), m.start(), m.end()))

    # 3) angka jutaan dengan pemisah ribuan tanpa "Rp" (mis. "12.500.000")
    for m in re.finditer(r"\b(\d{1,3}(?:\.\d{3}){1,3})\b", text):
        amount = _parse_number(m.group(1))
        if amount is None:
            continue
        results.append(PriceMatch(int(amount), m.group(0), m.start(), m.end()))

    # buang yang di luar rentang, atau yang posisinya jatuh di dalam model iPhone
    cleaned: list[PriceMatch] = []
    for item in results:
        if not (min_amount <= item.amount <= max_amount):
            continue
        # Kalau ada penanda mata uang/multiplier ("Rp", "jt", "rb"), itu harga -
        # jangan pernah dianggap bagian dari nama model.
        explicit_currency = bool(
            re.search(r"(rp\.?|idr|jt|juta|ribu|\brb\b)", item.raw, re.I)
        )
        if not explicit_currency and _inside_iphone_model(text, item.start, item.end):
            continue
        cleaned.append(item)

    # buang tumpang tindih (pertahankan yang paling spesifik/pertama)
    cleaned.sort(key=lambda p: (p.start, -(p.end - p.start)))
    final: list[PriceMatch] = []
    for item in cleaned:
        if any(not (item.end <= other.start or item.start >= other.end) for other in final):
            continue
        final.append(item)
    return final


_IPHONE_CONTEXT = re.compile(r"\b(iphone|ip\s?\d|ip\d)", re.I)


def _inside_iphone_model(text: str, start: int, end: int) -> bool:
    """Hindari angka pada 'iPhone 11' / '13 Pro' dianggap harga.

    Hanya untuk angka pendek tanpa pemisah (mis. 11, 13, 15). Angka seperti
    12.500.000 tidak pernah masuk ke sini karena ada pemisah ribuan.
    """
    token = text[start:end]
    if not re.fullmatch(r"\d{1,2}", token):
        return False
    before = text[max(0, start - 16) : start].lower()
    after = text[end : end + 14].lower()
    if re.search(r"\b(iphone|ip)\s*$", before):
        return True
    if re.match(r"^\s*(pro|max|mini|plus|pm|pro max|promax)\b", after) and re.search(
        r"\b(iphone|ip\s?\d{1,2})\b", text[:start], re.I
    ):
        return True
    return False


def detect_price_kind(text: str) -> str:
    """Tentukan apakah angka adalah harga jual atau budget pembeli."""
    lowered = (text or "").lower()
    # Catatan: "max" sendirian TIDAK dipakai karena sering muncul di "Pro Max".
    budget_hits = re.findall(
        r"\b(budget|bujet|maks|maksimal|kisaran|cari|dicari|butuh|wtb|looking for|nemu)\b"
        r"|\b(di ?bawah|sampai)\b",
        lowered,
    )
    price_hits = re.findall(
        r"\b(harga|dijual|jual|for sale|wts|nego|nett|pas|open order|ready stock)\b",
        lowered,
    )
    if len(budget_hits) > len(price_hits):
        return PriceKind.BUDGET.value
    if len(price_hits) > len(budget_hits):
        return PriceKind.ASKING.value
    return PriceKind.UNKNOWN.value


# --------------------------------------------------------------------------
# Waktu posting
# --------------------------------------------------------------------------

_MONTHS_ID = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "agu": 8, "agt": 8,
    "sep": 9, "okt": 10, "nov": 11, "des": 12,
}
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_RELATIVE_RE = re.compile(
    r"(\d+)\s*(menit|mnt|jam|hari|hr|minggu|minggu|bulan|tahun)\s*(yang\s+lalu|lalu)?",
    re.I,
)
_ABSOLUTE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s+(\d{1,2})[.:](\d{2}))?",
)


def parse_posted_at(
    raw: str | None, reference: datetime | None = None
) -> tuple[str | None, str, str | None]:
    """Tafsirkan waktu posting bila cukup andal.

    Mengembalikan (iso_utc | None, presisi, catatan).
    presisi: "tepat" (tanggal & jam eksplisit), "perkiraan" (relatif), "tidak diketahui".

    Waktu relatif ("3 jam lalu") HANYA perkiraan karena dihitung dari waktu ditemukan.
    """
    if not raw:
        return None, UNKNOWN, None
    text = collapse_ws(raw)
    ref = reference or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    # ISO langsung
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    if iso:
        try:
            dt = datetime(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3)),
                int(iso.group(4)), int(iso.group(5)), tzinfo=timezone.utc,
            )
            return dt.isoformat(), "tepat", "Dibaca dari format ISO."
        except ValueError:
            pass

    # "5 November 2025 14:30" / "5 Nov 2025"
    abs_match = _ABSOLUTE_RE.search(text)
    if abs_match:
        day = int(abs_match.group(1))
        month_name = abs_match.group(2).lower()
        month = _MONTHS_ID.get(month_name) or _MONTHS_EN.get(month_name)
        year = int(abs_match.group(3))
        if month:
            try:
                dt = datetime(
                    year, month, day,
                    int(abs_match.group(4) or 0), int(abs_match.group(5) or 0),
                    tzinfo=timezone.utc,
                )
                note = "Tanggal eksplisit; jam tidak disebut, diisi 00:00." if not abs_match.group(4) else "Tanggal & jam eksplisit."
                return dt.isoformat(), ("tepat" if abs_match.group(4) else "perkiraan"), note
            except ValueError:
                pass

    # relatif: "3 jam lalu"
    rel = _RELATIVE_RE.search(text)
    if rel and (rel.group(3) or re.match(r"^\s*(\d+)\s*(menit|jam|hari|minggu)", text, re.I)):
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        deltas = {
            "menit": timedelta(minutes=amount), "mnt": timedelta(minutes=amount),
            "jam": timedelta(hours=amount),
            "hari": timedelta(days=amount), "hr": timedelta(days=amount),
            "minggu": timedelta(weeks=amount),
            "bulan": timedelta(days=30 * amount),
            "tahun": timedelta(days=365 * amount),
        }
        delta = deltas.get(unit)
        if delta is not None:
            dt = ref - delta
            return (
                dt.replace(second=0, microsecond=0).isoformat(),
                "perkiraan",
                f"Dihitung dari teks relatif '{text}'. Bisa meleset.",
            )

    return None, UNKNOWN, "Format waktu tidak dikenali."
