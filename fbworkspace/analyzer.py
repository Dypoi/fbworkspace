"""Analisis postingan iPhone BERBASIS ATURAN (tanpa AI).

Prinsip:
1. Teks postingan adalah DATA TIDAK TEPERCAYA. Tidak ada perintah yang dijalankan,
   tidak ada kode yang di-eval.
2. Tidak menebak. Field yang tidak disebutkan di teks -> "tidak diketahui".
3. "Skor relevansi" adalah skor kriteria yang bisa dijelaskan (0-100),
   BUKAN probabilitas kebenaran dan bukan penilaian kualitas penjual.

Kriteria skor (ditambahkan, maksimal 100):
  +40  teks benar-benar menyebut iPhone/iPad? tidak — hanya iPhone yang dihitung
  +15  model iPhone teridentifikasi (mis. "iPhone 13 Pro Max")
  +20  kategori "mencari" atau "menjual" (ada niat transaksi)
   +5  kategori "diskusi" (masih relevan, tapi bukan transaksi)
  +10  ada nominal harga/budget yang terbaca
  +10  ada lokasi yang dikenali dari daftar
   +5  ada sinyal transaksi tambahan (COD/nego/DM/stok/garansi)
Teks tanpa penyebutan iPhone tidak pernah melewati 20 poin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .locations import LOCATION_ALIASES, known_locations
from .models import UNKNOWN, AnalysisResult, Category, PriceKind
from .normalize import (
    collapse_ws,
    detect_price_kind,
    find_prices,
    normalize_text,
    truncate,
)

# ---------------------------------------------------------------------------
# Kosakata sinyal
# ---------------------------------------------------------------------------

# Bentuk yang dihitung "menyebut iPhone" (termasuk singkatan pasar Indonesia):
#   "iphone", "iphone 13", "iphonex", "iphone x", "iphone xr",
#   "ip 11", "ip13pm", "ip x", "ipx", "ipxs"
# "ipad" dan kata "ip" sendirian TIDAK dihitung.
_IPHONE_STRONG_RE = re.compile(
    r"\b(?:iphone(?:\s*(?:xr|xs|x))?|ip\s?(?:\d{1,2}(?:\s?(?:pm|pro|plus|pl|max))?|xr|xs|x))\b",
    re.I,
)
_IPHONE_RE = _IPHONE_STRONG_RE


def mentions_iphone(text: str) -> bool:
    """True bila teks menyebut iPhone, termasuk singkatan (ip x, ipx, iphonex)."""
    return bool(_IPHONE_STRONG_RE.search(normalize_text(text)))

# Kata bersinyal kuat (bobot 2) vs lemah (bobot 1).
_SELL_STRONG = [
    r"\bdijual\b", r"\bdijual cepat\b", r"\bjual\b", r"\bwts\b", r"\bfor sale\b",
    r"\bopen order\b", r"\bready stock\b", r"\bstok\b", r"\bsiap kirim\b",
    r"\bharga\b", r"\bopen po\b",
]
_SELL_WEAK = [
    r"\bnego\b", r"\bcod\b", r"\bminat\b", r"\bdm\b", r"\bwa\b", r"\bunit\b",
    r"\bgaransi\b", r"\bbnib\b", r"\bsegel\b", r"\bfullset\b", r"\bnett\b",
]
_BUY_STRONG = [
    r"\bcari\b", r"\bdicari\b", r"\bnyari\b", r"\bwtb\b", r"\bbutuh\b",
    r"\bmau beli\b", r"\bingin beli\b", r"\bpengen beli\b", r"\blooking for\b",
    r"\bada yang jual\b", r"\bsiapa yang jual\b", r"\byang mau jual\b",
    r"\bbudget\b", r"\bbujet\b", r"\bmaks\b", r"\bmaksimal\b",
]
_BUY_WEAK = [r"\brekomendasi\b", r"\bsaran\b", r"\bminat beli\b", r"\bpencarian\b"]
_DISCUSSION_SIGNALS = [
    r"\bapakah\b", r"\bapa\b", r"\bkenapa\b", r"\bkok\b", r"\bbagaimana\b",
    r"\bmenurut\b", r"\bpendapat\b", r"\breview\b", r"\bpengalaman\b",
    r"\btanya\b", r"\bdiskusi\b", r"\bworth it\b", r"\bbagus mana\b",
    r"\bvs\b", r"\bsolusinya\b", r"\bproblem\b", r"\bmasalah\b",
]
_TRANSACTION_EXTRA = [r"\bcod\b", r"\bnego\b", r"\bdm\b", r"\bstok\b", r"\bgaransi\b"]

# ---------------------------------------------------------------------------
# Model iPhone
# ---------------------------------------------------------------------------

_MODEL_WORDS = {
    "pro max": "Pro Max", "promax": "Pro Max", "pm": "Pro Max",
    "pro": "Pro", "plus": "Plus", "mini": "Mini", "max": "Max", "e": "e",
}

_MODEL_RE = re.compile(
    r"\b(?:iphone|ip)\s*(?P<gen>1[0-9]|[4-9]|xr|xs|x|se|air)?"
    r"\s*(?P<variant>pro\s*max|promax|pro|plus|mini|max|pm|e|air)?\b",
    re.I,
)
_MODEL_COMPACT_RE = re.compile(
    r"\bip\s?(?P<gen>1[0-9]|[4-9]|xr|xs|x|se)(?P<variant>pm|pro|plus)?\b", re.I
)
_MODEL_INLINE_RE = re.compile(
    r"\b(?P<gen>1[0-9])\s*(?P<variant>pro\s*max|pro|plus|mini)\b", re.I
)

_MODEL_WHITELIST = {
    "4", "5", "6", "7", "8", "x", "xr", "xs", "se", "11", "12", "13", "14",
    "15", "16", "17",
}


def extract_iphone_model(text: str) -> str:
    """Ambil model iPhone yang tertulis eksplisit. Kembalikan 'tidak diketahui' bila ragu."""
    if not text:
        return UNKNOWN
    has_iphone = bool(_IPHONE_STRONG_RE.search(text))

    # 1) Bentuk ringkas: ip13pm, ip11pro
    for m in _MODEL_COMPACT_RE.finditer(text):
        gen = m.group("gen").lower()
        if gen not in _MODEL_WHITELIST:
            continue
        variant = (m.group("variant") or "").lower()
        return _build_model(gen, variant)

    # 2) Bentuk umum: iphone 13 pro max / iphone xr / iphone se
    for m in _MODEL_RE.finditer(text):
        gen = (m.group("gen") or "").lower()
        variant = (m.group("variant") or "").lower().replace(" ", "")
        if not gen:
            continue
        if gen == "se":
            return "iPhone SE"
        if gen in {"xr", "xs", "x"}:
            if gen == "xs" and variant in {"max", "pm", "promax", "pro max"}:
                return "iPhone XS Max"
            return f"iPhone {gen.upper()}"
        if gen == "air":
            return "iPhone Air"
        if gen not in _MODEL_WHITELIST:
            continue
        return _build_model(gen, variant)

    # 3) "13 pro max" tanpa kata iPhone, hanya jika ada konteks iPhone di teks
    if has_iphone:
        for m in _MODEL_INLINE_RE.finditer(text):
            gen = m.group("gen")
            if gen not in _MODEL_WHITELIST:
                continue
            return _build_model(gen, m.group("variant").lower().replace(" ", ""))
    return UNKNOWN


def _build_model(gen: str, variant: str) -> str:
    gen = gen.lower()
    if gen in {"xr", "xs", "x", "se"}:
        base = f"iPhone {gen.upper()}"
        if gen == "xs" and variant in {"max", "pm", "promax", "pro max"}:
            return base + " Max"
        return base
    if variant in {"e"}:
        return f"iPhone {gen}e"
    if variant == "air":
        return "iPhone Air"
    label = {"pm": "Pro Max", "promax": "Pro Max", "pro": "Pro", "plus": "Plus",
             "mini": "Mini", "max": "Max"}.get(variant, "")
    return f"iPhone {gen}{(' ' + label) if label else ''}".strip()


# ---------------------------------------------------------------------------
# Detail perangkat
# ---------------------------------------------------------------------------

_STORAGE_RE = re.compile(r"\b(?P<size>32|64|128|256|512)\s?(?:gb|g\b)|\b(?P<tb>1|2)\s?tb\b", re.I)
_BATTERY_RE = re.compile(
    r"(?:\bbh\b|battery\s*health|kesehatan\s*baterai|baterai)\s*[:=]?\s*(?P<pct>\d{2,3})\s?%",
    re.I,
)
_WARRANTY_RES = [
    (re.compile(r"\bgaransi\s+(resmi|ibox|i-box|inter|distributor)\b", re.I), "garansi"),
    (re.compile(r"\btanpa\s+garansi\b|\bno\s+garansi\b|\bnon\s+garansi\b", re.I), "tanpa garansi"),
    (re.compile(r"\bgaransi\s+sampai\b[^\n,.]{0,30}", re.I), "garansi"),
]
_CONDITION_WORDS = [
    "mulus", "minus", "bekas", "second", "bnib", "segel", "fullset", "full set",
    "icloud", "blacklist", "ex inter", "ex-inter", "like new", "normal", "lecet",
    "baret", "bootloop", "mati", "rusak", "orisinil", "ori",
]


def extract_details(text: str) -> dict[str, str]:
    """Ambil detail perangkat HANYA jika disebutkan."""
    details: dict[str, str] = {}
    lowered = (text or "").lower()

    storage = _STORAGE_RE.search(text or "")
    if storage:
        if storage.group("size"):
            details["storage"] = f"{storage.group('size')} GB"
        elif storage.group("tb"):
            details["storage"] = f"{storage.group('tb')} TB"

    battery = _BATTERY_RE.search(text or "")
    if battery:
        try:
            pct = int(battery.group("pct"))
            if 1 <= pct <= 100:
                details["battery_health"] = f"{pct}%"
        except ValueError:
            pass

    for pattern, _label in _WARRANTY_RES:
        m = pattern.search(text or "")
        if m:
            details["garansi"] = collapse_ws(m.group(0))
            break

    conditions = [w for w in _CONDITION_WORDS if re.search(rf"\b{re.escape(w)}\b", lowered)]
    if conditions:
        details["kondisi"] = ", ".join(dict.fromkeys(conditions))

    if re.search(r"\bicloud\s*(on|aktif|nyala)\b", lowered):
        details["icloud"] = "disebut aktif/on"
    elif re.search(r"\bicloud\s*(off|mati|clear|bersih)\b", lowered):
        details["icloud"] = "disebut off/clear"

    if re.search(r"\bblacklist\b", lowered):
        details["status_perangkat"] = "disebut blacklist"
    if re.search(r"\bbypass\b", lowered):
        details["status_perangkat"] = "disebut bypass (hati-hati)"

    return details


# ---------------------------------------------------------------------------
# Lokasi & kategori
# ---------------------------------------------------------------------------

def extract_location(text: str) -> str:
    """Cari nama lokasi yang tercantum eksplisit. Tidak menyimpulkan dari nomor telepon."""
    if not text:
        return UNKNOWN
    haystack = f" {text} "
    for loc in known_locations():
        if re.search(rf"(?<![\w]){re.escape(loc)}(?![\w])", haystack, re.I):
            return loc
    for alias, canonical in LOCATION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", haystack, re.I):
            return canonical
    return UNKNOWN


def _count_signals(text: str, patterns: list[str]) -> tuple[int, list[str]]:
    hits: list[str] = []
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            hits.append(m.group(0).lower())
    return len(hits), list(dict.fromkeys(hits))


def classify_category(text: str) -> tuple[str, dict[str, int]]:
    """Tentukan kategori dari sinyal kata (berbobot), bukan AI.

    Kata kuat bernilai 2, kata lemah 1. Contoh: "cari iPhone, budget 4jt, bisa COD"
    tetap dianggap PEMBELI walaupun ada kata COD.
    """
    if not _IPHONE_STRONG_RE.search(text):
        return Category.IRRELEVANT.value, {"menjual": 0, "mencari": 0, "diskusi": 0}

    sell_strong = _count_signals(text, _SELL_STRONG)
    sell_weak = _count_signals(text, _SELL_WEAK)
    buy_strong = _count_signals(text, _BUY_STRONG)
    buy_weak = _count_signals(text, _BUY_WEAK)
    discussion = _count_signals(text, _DISCUSSION_SIGNALS)

    sell_score = sell_strong[0] * 2 + sell_weak[0]
    buy_score = buy_strong[0] * 2 + buy_weak[0]
    counts = {"menjual": sell_score, "mencari": buy_score, "diskusi": discussion[0]}

    is_question = "?" in text or bool(re.search(r"\b(apakah|bagaimana|kenapa|kok)\b", text, re.I))

    if buy_score > sell_score and buy_score > 0:
        return Category.BUYING.value, counts
    if sell_score > buy_score and sell_score > 0:
        return Category.SELLING.value, counts
    if buy_score == sell_score and buy_score > 0:
        # Seri: frasa seperti "ada yang jual ...?" adalah pembeli,
        # meskipun kata "jual" ikut terhitung sebagai sinyal penjual.
        if re.search(r"\b(ada|siapa)\s+yang\s+jual\b|\byang\s+mau\s+jual\b", text, re.I):
            return Category.BUYING.value, counts
        if buy_strong[0] and not sell_strong[0]:
            return Category.BUYING.value, counts
        return Category.SELLING.value, counts
    if discussion[0] > 0 or is_question:
        return Category.DISCUSSION.value, counts
    return Category.DISCUSSION.value, counts


# ---------------------------------------------------------------------------
# Analisis utama
# ---------------------------------------------------------------------------

def analyze_text(text: str, engine: str = "rule") -> AnalysisResult:
    """Analisis satu postingan dengan aturan."""
    clean = normalize_text(text)
    if not clean:
        return AnalysisResult(
            category=Category.IRRELEVANT.value,
            reasons=["Teks kosong."],
            relevance_score=0,
            engine=engine,
        )

    iphone_mentioned = bool(_IPHONE_STRONG_RE.search(clean))
    model = extract_iphone_model(clean)
    category, signal_counts = classify_category(clean)
    prices = find_prices(clean)
    price_amount = prices[0].amount if prices else None
    price_kind = detect_price_kind(clean) if price_amount else PriceKind.UNKNOWN.value
    location = extract_location(clean)
    details = extract_details(clean)

    score = 0
    reasons: list[str] = []

    if iphone_mentioned:
        score += 40
        reasons.append("+40 menyebut iPhone.")
    else:
        reasons.append("Tidak menyebut iPhone: tidak termasuk target pencarian.")

    if model != UNKNOWN:
        score += 15
        reasons.append(f"+15 model teridentifikasi: {model}.")

    if category in {Category.BUYING.value, Category.SELLING.value}:
        score += 20
        reasons.append(f"+20 kategori '{category}' (ada niat transaksi).")
    elif category == Category.DISCUSSION.value:
        score += 5
        reasons.append("+5 kategori diskusi (relevan, bukan transaksi).")

    if price_amount:
        score += 10
        reasons.append(
            f"+10 nominal terbaca: Rp{price_amount:,} ({'budget pembeli' if price_kind == PriceKind.BUDGET.value else 'harga'}); "
            "pembeda harga/budget ditentukan dari kata kunci di teks."
        )
    if location != UNKNOWN:
        score += 10
        reasons.append(f"+10 lokasi disebut: {location}.")
    extra, extra_hits = _count_signals(clean, _TRANSACTION_EXTRA)
    if extra:
        score += 5
        reasons.append(f"+5 sinyal transaksi tambahan: {', '.join(extra_hits[:4])}.")

    if not iphone_mentioned:
        score = min(score, 20)
    score = max(0, min(score, 100))

    if score == 0:
        reasons.append("Skor 0: tidak ada kriteria yang terpenuhi.")

    return AnalysisResult(
        category=category,
        iphone_model=model,
        price_amount=price_amount,
        price_kind=price_kind,
        location=location,
        details=details,
        relevance_score=score,
        reasons=reasons,
        snippet=_make_snippet(clean),
        engine=engine,
    )


def _make_snippet(text: str, width: int = 220) -> str:
    """Cuplikan pendek di sekitar penyebutan iPhone pertama."""
    match = _IPHONE_STRONG_RE.search(text)
    if not match:
        return truncate(collapse_ws(text), width)
    start = max(0, match.start() - width // 3)
    end = min(len(text), start + width)
    piece = collapse_ws(text[start:end])
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{piece}{suffix}"


# ---------------------------------------------------------------------------
# Validasi keluaran model AI (dipakai modul Ollama)
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = {c.value for c in Category}


@dataclass
class ValidationResult:
    ok: bool
    data: dict | None
    errors: list[str]


def validate_llm_payload(payload: object, source_text: str = "") -> ValidationResult:
    """Validasi ketat keluaran Ollama sebelum disimpan.

    Aturan:
    - harus dict dengan kunci yang dikenal
    - kategori harus salah satu nilai tetap
    - skor integer 0-100 (di-clamp, bukan dipercaya mentah)
    - field lain harus string pendek / angka wajar
    - jika model tidak menyebut iPhone padahal teksnya tidak menyebut iPhone,
      kategori dipaksa "tidak relevan"
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ValidationResult(False, None, ["Keluaran bukan objek JSON."])

    category = str(payload.get("category", "")).strip().lower()
    if category not in _VALID_CATEGORIES:
        errors.append(f"Kategori tidak valid: {category!r}")
        category = Category.IRRELEVANT.value

    def _text_field(key: str, limit: int = 80, default: str = UNKNOWN) -> str:
        value = payload.get(key)
        if value is None:
            return default
        if not isinstance(value, (str, int, float)):
            errors.append(f"Field {key} bukan teks.")
            return default
        value = collapse_ws(str(value))
        if value == "":
            return default
        if len(value) > limit:
            value = truncate(value, limit)
        return value

    model = _text_field("iphone_model")
    if model != UNKNOWN and not re.search(r"iphone", model, re.I):
        model = f"iPhone {model}" if re.fullmatch(r"[\w\s.+-]{1,30}", model) else UNKNOWN

    price_amount = payload.get("price_amount")
    if price_amount in (None, "", UNKNOWN, "null"):
        price_amount = None
    else:
        try:
            price_amount = int(float(str(price_amount).replace(".", "").replace(",", "").split()[0]))
            if not (0 < price_amount <= 500_000_000):
                errors.append("price_amount di luar rentang wajar, diabaikan.")
                price_amount = None
        except (ValueError, IndexError):
            errors.append("price_amount tidak bisa dibaca sebagai angka, diabaikan.")
            price_amount = None

    price_kind = _text_field("price_kind", 20)
    if price_kind not in {p.value for p in PriceKind}:
        price_kind = PriceKind.UNKNOWN.value

    score = payload.get("relevance_score", 0)
    try:
        score = int(float(score))
    except (TypeError, ValueError):
        errors.append("relevance_score tidak bisa dibaca, diset 0.")
        score = 0
    score = max(0, min(score, 100))

    details = payload.get("details") or {}
    if not isinstance(details, dict):
        errors.append("details bukan objek, diabaikan.")
        details = {}
    cleaned_details = {
        str(k)[:30]: truncate(collapse_ws(str(v)), 60)
        for k, v in list(details.items())[:10]
        if isinstance(v, (str, int, float)) and str(v).strip()
    }

    reasons = payload.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        errors.append("reasons bukan daftar, diabaikan.")
        reasons = []
    reasons = [truncate(collapse_ws(str(r)), 200) for r in reasons[:6]]

    snippet = _text_field("snippet", 300, default="")

    # Pengaman: model tidak boleh "menemukan" iPhone yang tidak ada di teks
    if source_text and not _IPHONE_STRONG_RE.search(source_text):
        if category != Category.IRRELEVANT.value:
            errors.append("Teks sumber tidak menyebut iPhone; kategori dipaksa 'tidak relevan'.")
        category = Category.IRRELEVANT.value
        score = min(score, 20)

    data = AnalysisResult(
        category=category,
        iphone_model=model,
        price_amount=price_amount,
        price_kind=price_kind,
        location=_text_field("location", 80),
        details=cleaned_details,
        relevance_score=score,
        reasons=reasons or ["Model tidak memberi alasan."],
        snippet=snippet or _make_snippet(source_text),
        engine="ollama",
    ).as_dict()

    # Field wajib harus ada; kalau ada error struktur -> tetap tolak
    fatal = [e for e in errors if "bukan objek JSON" in e]
    return ValidationResult(not fatal, data, errors)
