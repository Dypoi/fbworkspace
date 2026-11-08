"""Penyusun balasan chat DM: pertanyaan -> rencana balasan.

Batas keras modul ini:
1. Tidak pernah mengirim apa pun. Hanya menghasilkan teks/rencana.
2. Harga hanya dari :mod:`fbworkspace.pricelist`. Model/storage yang tidak ada
   di daftar -> "diam" + eskalasi ke pemilik, bukan tebakan.
3. Di luar template (nego, tukar tambah, komplain, dll.) -> "diam" + eskalasi.
4. Kalimat penolakan/klaim karangan dicek lewat ``contains_forbidden_claim``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .drafts import contains_forbidden_claim
from .normalize import collapse_ws
from .pricelist import PriceEntry, find_entries, find_exact, is_ready, storages_for

# Kalimat tetap yang sudah Anda setujui
LOCATION_TEXT = "Kutabumi, Tangerang"
WHATSAPP_TEXT = "081513839973"

MAX_REPLY_CHARS = 400

ACTION_SEND = "kirim"
ACTION_SILENT = "diam"

# --------------------------------------------------------------------------
# Pengenalan model, storage, dan maksud
# --------------------------------------------------------------------------

_MODEL_EXPLICIT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iphone se gen2", re.compile(r"\b(?:iphone\s*)?se(?:\s*(?:gen\s*2|gen2|2020|2))?\b", re.I)),
    ("iphone xr", re.compile(r"\b(?:iphone\s*)?xr\b", re.I)),
    ("iphone 11", re.compile(r"\b(?:iphone|ip|i)\s*[- ]?\s*11(?:\s*(?:pro|promax|pro\s*max|plus|mini))?\b", re.I)),
    ("iphone 8", re.compile(r"\b(?:iphone|ip|i)\s*[- ]?\s*8(?:\s*(?:plus))\b|\b(?:iphone|ip|i)\s*[- ]?\s*8\b", re.I)),
)

# Model yang sering ditulis tanpa awalan ("11 64gb ready?")
_MODEL_BARE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iphone se gen2", re.compile(r"\bse\s*(?:gen\s*2|gen2|2020)\b", re.I)),
    ("iphone xr", re.compile(r"\bxr\b", re.I)),
    ("iphone 11", re.compile(r"\b11\b")),
    ("iphone 8", re.compile(r"\b8\b")),
)

_UNKNOWN_MODEL_RE = re.compile(
    r"\b(?:iphone|ip)\s*[- ]?\s*(1[0-9]|[6-9]|x[sr]?|se)\b"
    r"(\s*(?:pro|promax|pro\s*max|plus|mini))?",
    re.I,
)

_STORAGE_RE = re.compile(r"\b(16|32|64|128|256|512)\s*(?:gb|g)\b", re.I)
_STORAGE_BARE_RE = re.compile(r"\b(64|128|256|512)\b")

_TIME_WORDS_RE = re.compile(r"\b(jam|menit|hari|tanggal|tgl|bulan|tahun|minggu)\b", re.I)

_INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "harga": re.compile(
        r"\b(harga|hargaa|brp|berapa|berapaan|mahar|price|pricelist|pl)\b", re.I
    ),
    "ready": re.compile(r"\b(ready|redy|stok|stock|masih\s+ada|masih)\b", re.I),
    "lokasi": re.compile(
        r"\b(cod|lokasi|alamat|dimana|di\s*mana|ketemuan|tempat|tmpt|wilayah|daerah)\b", re.I
    ),
    "wa": re.compile(
        r"\b(wa|whatsapp|nomor|nomer|no\s*hp|tlp|telp|telepon|kontak|hubungi)\b", re.I
    ),
    "nego": re.compile(
        r"\b(nego|negor|negoan|kurang|kurangin|diskon|tawar|tt|tukar\s*tambah|barter)\b", re.I
    ),
}

_ACK_RE = re.compile(
    r"\b(ok|oke|okay|okey|sip|siap|makasih|terima\s*kasih|thanks|thank\s*you|p|ping|"
    r"halo|hai|hi|hallo|assalamualaikum|pagi|siang|malam|sore)\b",
    re.I,
)

_PHONE_RE = re.compile(r"\b(0|\+62|62)8\d{7,12}\b")
_ASK_OUR_NUMBER_RE = re.compile(
    r"\b(minta|mnt|minta\s+no|nomor\s*(?:kamu|anda|nya)|no\s*(?:wa|hp)?\s*(?:kamu|anda|nya)|"
    r"wa\s*(?:kamu|anda|nya)|kontak\s*(?:kamu|anda|nya))\b",
    re.I,
)
_BARE_ADA_RE = re.compile(r"\bada\b", re.I)


@dataclass(frozen=True)
class ReplyPlan:
    """Hasil analisis satu pesan masuk."""

    action: str = ACTION_SILENT
    body: str = ""
    escalate: bool = False
    reason: str = ""
    intents: tuple[str, ...] = ()
    model_key: str | None = None
    storage: int | None = None

    @property
    def should_send(self) -> bool:
        return self.action == ACTION_SEND and bool(self.body.strip())


@dataclass
class MatchResult:
    model_key: str | None = None
    mention: str | None = None
    known: bool = False
    extra: dict = field(default_factory=dict)


def _clean(text: str | None) -> str:
    return collapse_ws((text or "").lower())


def detect_intents(text: str) -> tuple[str, ...]:
    value = _clean(text)
    return tuple(name for name, pattern in _INTENT_PATTERNS.items() if pattern.search(value))


def detect_model(text: str, *, allow_bare: bool = True) -> MatchResult:
    """Kenali model iPhone yang disebut.

    ``known`` False + ``mention`` terisi = ada iPhone disebut tapi tidak ada di
    daftar harga (mis. "iphone 14 pro") -> perlu eskalasi.
    """
    value = _clean(text)
    for key, pattern in _MODEL_EXPLICIT:
        match = pattern.search(value)
        if match:
            # "iphone 11 pro" bukan model yang ada di daftar
            suffix = match.group(0)
            if re.search(r"\b(pro|max|plus|mini)\b", suffix) and not suffix.startswith("8"):
                return MatchResult(model_key=None, mention=collapse_ws(suffix), known=False)
            return MatchResult(model_key=key, mention=collapse_ws(suffix), known=True)

    if allow_bare and _bare_allowed(value):
        for key, pattern in _MODEL_BARE:
            match = pattern.search(value)
            if match:
                return MatchResult(model_key=key, mention=match.group(0), known=True)

    match = _UNKNOWN_MODEL_RE.search(value)
    if match:
        return MatchResult(model_key=None, mention=collapse_ws(match.group(0)), known=False)
    return MatchResult()


def _bare_allowed(value: str) -> bool:
    """Angka telanjang hanya dipercaya pada pesan pendek tanpa kata waktu."""
    if not value or len(value) > 40:
        return False
    if _TIME_WORDS_RE.search(value):
        return False
    if re.search(r"\d{5,}", value):  # harga
        return False
    return True


def detect_storage(text: str, *, allow_bare: bool = True) -> int | None:
    value = _clean(text)
    match = _STORAGE_RE.search(value)
    if match:
        return int(match.group(1))
    if allow_bare and _bare_allowed(value):
        match = _STORAGE_BARE_RE.search(value)
        if match:
            return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# Penyusunan balasan
# --------------------------------------------------------------------------


def _storage_question(options: list[int]) -> str:
    labels = [f"{opt}GB" for opt in options]
    if len(labels) == 1:
        return f"Storage {labels[0]} ya kak?"
    if len(labels) == 2:
        return f"Storage-nya {labels[0]} atau {labels[1]} kak?"
    return "Storage-nya " + ", ".join(labels[:-1]) + f", atau {labels[-1]} kak?"


def compose_reply(message: str, entries: list[PriceEntry]) -> ReplyPlan:
    """Ubah satu pesan masuk menjadi rencana balasan (tanpa mengirim)."""
    text = _clean(message)
    if not text:
        return ReplyPlan(reason="pesan kosong")

    intents = detect_intents(text)
    model = detect_model(text)
    storage = detect_storage(text)
    wants_ready = "ready" in intents or bool(
        model.model_key and len(text) <= 30 and _BARE_ADA_RE.search(text)
    )
    parts: list[str] = []
    escalate = False
    reasons: list[str] = []

    if "nego" in intents:
        escalate = True
        reasons.append("nego")

    if _PHONE_RE.search(text):
        escalate = True
        reasons.append("pembeli mengirim nomor telepon")
        if not _ASK_OUR_NUMBER_RE.search(text):
            return ReplyPlan(
                reason="pembeli mengirim nomor telepon (perlu Anda hubungi)",
                escalate=True,
                intents=intents,
                storage=storage,
            )

    # --- Harga -----------------------------------------------------------
    if "harga" in intents:
        if not model.model_key:
            if model.mention:
                return ReplyPlan(
                    reason=f"model belum ada di daftar harga: {model.mention}",
                    escalate=True,
                    intents=intents,
                    storage=storage,
                )
            return ReplyPlan(
                reason="tanya harga tanpa model yang jelas",
                escalate=True,
                intents=intents,
            )
        variants = find_entries(entries, model.model_key)
        if storage is not None:
            exact = find_exact(entries, model.model_key, storage)
            if exact is None:
                return ReplyPlan(
                    reason=f"harga {model.model_key} {storage}GB belum ada di daftar",
                    escalate=True,
                    intents=intents,
                    model_key=model.model_key,
                    storage=storage,
                )
            parts.append(f"{exact.price_text} kak")
        elif len(variants) == 1:
            parts.append(f"{variants[0].price_text} kak")
        elif variants:
            options = storages_for(entries, model.model_key)
            if options:
                parts.append(_storage_question(options))
            else:
                parts.append(f"{variants[0].price_text} kak")

    # --- Ready / stok ----------------------------------------------------
    if wants_ready:
        if model.model_key:
            if storage is not None and find_exact(entries, model.model_key, storage) is None:
                return ReplyPlan(
                    reason=f"{model.model_key} {storage}GB belum ada di daftar",
                    escalate=True,
                    intents=intents,
                    model_key=model.model_key,
                    storage=storage,
                )
            if is_ready(entries, model.model_key, storage):
                parts.append("Ready kak")
            else:
                parts.append("Lagi kosong kak")
        elif any(entry.ready for entry in entries):
            parts.append("Ready kak")

    # --- Lokasi & nomor --------------------------------------------------
    if "lokasi" in intents:
        parts.append(LOCATION_TEXT)
    if "wa" in intents:
        parts.append(WHATSAPP_TEXT)

    if parts:
        body = "\n".join(dict.fromkeys(part for part in parts if part))[:MAX_REPLY_CHARS]
        banned = contains_forbidden_claim(body)
        if banned:
            return ReplyPlan(
                reason=f"balasan memuat klaim terlarang: {', '.join(banned)}",
                escalate=True,
                intents=intents,
                model_key=model.model_key,
                storage=storage,
            )
        return ReplyPlan(
            action=ACTION_SEND,
            body=body,
            escalate=escalate,
            reason="; ".join(reasons),
            intents=intents,
            model_key=model.model_key,
            storage=storage,
        )

    if escalate:
        return ReplyPlan(
            reason="; ".join(reasons) or "di luar template",
            escalate=True,
            intents=intents,
            model_key=model.model_key,
            storage=storage,
        )

    if "harga" in intents or "ready" in intents or "lokasi" in intents or "wa" in intents:
        # Sudah ditangani di atas; cadangan bila tidak menghasilkan bagian apa pun.
        return ReplyPlan(
            reason="tidak ada bagian balasan yang aman",
            escalate=True,
            intents=intents,
            model_key=model.model_key,
            storage=storage,
        )

    if model.model_key and storage is not None:
        exact = find_exact(entries, model.model_key, storage)
        if exact is not None:
            return ReplyPlan(
                action=ACTION_SEND,
                body=f"{exact.price_text} kak",
                reason="model + storage disebut tanpa kata tanya",
                intents=intents,
                model_key=model.model_key,
                storage=storage,
            )

    if _ACK_RE.search(text):
        return ReplyPlan(reason="hanya sapaan/ucapan terima kasih", intents=intents)

    return ReplyPlan(
        reason="di luar template",
        escalate=True,
        intents=intents,
        model_key=model.model_key,
        storage=storage,
    )


def summarize_entries(entries: list[PriceEntry]) -> str:
    """Ringkasan daftar harga untuk ditampilkan di UI/WA."""
    if not entries:
        return "Daftar harga kosong."
    lines = []
    for entry in sorted(entries, key=lambda e: (e.model_key, e.storage or 0)):
        storage = f" {entry.storage}GB" if entry.storage else ""
        ready = "ready" if entry.ready else "kosong"
        lines.append(f"{entry.label}{storage}: {entry.price_text} ({ready})")
    return "\n".join(lines)
