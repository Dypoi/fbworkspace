"""Balasan DM yang ditulis Ollama, dengan penanda pembuka & penutup.

Berbeda dari :mod:`fbworkspace.reply_style` (yang hanya mengubah gaya kalimat
template), modul ini membiarkan model **menulis sendiri** isi balasan.

Aturan yang dijaga di sini:

- Jawaban hanya untuk pertanyaan yang memang dijawab worker (keputusan itu tetap
  milik :mod:`fbworkspace.replies`): kalau template memutuskan "diam" (di luar
  ranah, mis. "nego"), AI **tidak** dipakai.
- Pembuka ``hai Dypoi`` dan penutup ``terimakasih dypoi`` selalu ditambahkan
  sendiri oleh kode, bukan oleh model, supaya tidak pernah hilang/berubah.
- Fakta tetap diperiksa: nomor WA dan lokasi tidak boleh berubah, klaim terlarang
  tidak boleh muncul, dan daftar harga selalu ikut di prompt.
- Kalau model mati/lambat/keluar dari aturan -> kembalikan ``None``. Worker lalu
  memutuskan sendiri (bawaan: diam + beri tahu pemilik).

Semua ini tidak mengubah risiko harga: model boleh menyebut harga, jadi
``ai_freeform`` TIDAK boleh dipakai bila Anda tidak ingin risiko harga keliru.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .drafts import contains_forbidden_claim
from .normalize import collapse_ws
from .pricelist import PriceEntry, label_for
from .replies import (
    LOCATION_TEXT,
    MAX_REPLY_CHARS,
    WHATSAPP_TEXT,
    detect_model,
    detect_storage,
)
from .reply_style import _default_generate, _parse

OPENING = "hai Dypoi"
CLOSING = "terimakasih dypoi"

MIN_BODY_CHARS = 3
MAX_BODY_CHARS = MAX_REPLY_CHARS  # batas yang sama dengan balasan template

_SYSTEM = (
    "Kamu penjual HP bekas di Indonesia yang membalas chat Messenger pembeli.\n"
    "Gaya: ramah, singkat, bahasa sehari-hari (pakai 'kak'), maksimal tiga kalimat.\n"
    "ATURAN:\n"
    "- Jangan mengarang harga di luar daftar harga yang diberikan.\n"
    "- Jangan menjanjikan garansi, stok pasti, atau COD di luar Kutabumi, Tangerang.\n"
    "- Jangan menyebut nama toko, jam buka, atau kota lain.\n"
    "- Jangan memakai emoji, tanda kutip, atau format markdown.\n"
    "- Kalau informasinya tidak ada di data, katakan akan dicek pemilik.\n"
    'Balas hanya JSON: {"balasan": "..."}'
)

# Klaim yang dilarang juga di dalam balasan AI (sama dengan balasan template).
_EXTRA_BANNED = ("garansi", "toko", "official store", "gratis ongkir")


@dataclass(frozen=True)
class AiReply:
    """Hasil satu percobaan menulis balasan dengan AI."""

    text: str
    ok: bool
    reason: str = ""
    raw_body: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def wrap(body: str) -> str:
    """Bungkus isi balasan dengan pembuka & penutup yang tetap (tanpa dobel)."""
    inner = _clean(body)
    if not inner:
        return ""
    return f"{OPENING}, {inner} {CLOSING}"


def price_lines(entries: list[PriceEntry]) -> str:
    """Daftar harga untuk prompt: pasti, dari data pengguna."""
    lines = []
    for entry in entries:
        storage = f" {entry.storage}GB" if entry.storage else ""
        ready = "" if entry.ready else " (kosong)"
        lines.append(f"- {entry.label}{storage}: Rp {entry.price_text}{ready}")
    return "\n".join(lines)


def _context_summary(
    incoming: str, context: list[str] | None, entries: list[PriceEntry]
) -> list[str]:
    """Ringkas topik aktif untuk model, tanpa mengirim seluruh riwayat chat."""
    model = detect_model(incoming)
    model_line = ""
    if not model.model_key:
        for line in reversed(context or []):
            candidate = detect_model(line)
            if candidate.model_key or candidate.mention:
                model = candidate
                model_line = line
                break

    summary: list[str] = []
    if model.model_key:
        label = next(
            (entry.label for entry in entries if entry.model_key == model.model_key),
            label_for(model.model_key),
        )
        summary.append(f"Model aktif: {label}.")
        variants = [entry for entry in entries if entry.model_key == model.model_key]
        if variants:
            prices = "; ".join(
                f"{entry.storage}GB Rp {entry.price_text}"
                for entry in variants
                if entry.storage is not None
            )
            if prices:
                summary.append(f"Harga/varian model ini: {prices}.")
        storage = detect_storage(incoming)
        if storage is None:
            for line in reversed(context or []):
                storage = detect_storage(line)
                if storage is not None:
                    break
        if storage is not None:
            summary.append(f"Storage yang sedang ditanyakan: {storage}GB.")
    elif model.mention:
        summary.append(f"Model disebut tetapi tidak ada di daftar: {model.mention}.")

    # Hanya satu baris sebelumnya yang paling relevan, misalnya pertanyaan
    # storage kita; jangan kirim delapan pesan mentah ke Ollama.
    if context:
        relevant = model_line or context[-1]
        summary.append(f"Pesan relevan sebelumnya: {collapse_ws(relevant)[:160]}")
    return summary


def build_prompt(
    *,
    incoming: str,
    context: list[str] | None = None,
    entries: list[PriceEntry] | None = None,
) -> str:
    price_entries = entries or []
    parts = [_SYSTEM, "", "DAFTAR HARGA (satu-satunya sumber harga):"]
    parts.append(price_lines(price_entries) or "- (kosong)")
    parts.append("")
    parts.append(f"LOKASI: {LOCATION_TEXT}")
    parts.append(f"WHATSAPP: {WHATSAPP_TEXT}")
    summary = _context_summary(incoming, context, price_entries)
    if summary:
        parts.append("")
        parts.append("RINGKASAN TOPIK AKTIF (gunakan hanya jika relevan):")
        parts.extend(f"- {line}" for line in summary)
    parts.append("")
    parts.append(f"<<<PESAN PEMBELI>>>\n{collapse_ws(incoming)[:400]}\n<<<END>>>")
    parts.append("JSON:")
    return "\n".join(parts)


def _clean(body: str) -> str:
    text = collapse_ws((body or "").replace("\n", " ")).strip()
    # Buang penanda yang kadang ditulis model sendiri (agar tidak dobel).
    text = re.sub(rf"^\s*{re.escape(OPENING)}[\s,:-]*", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"[\s,:-]*{re.escape(CLOSING)}\s*$", "", text, flags=re.IGNORECASE)
    return text.strip(" .,-")


def generate_reply(
    *,
    incoming: str,
    context: list[str] | None = None,
    entries: list[PriceEntry] | None = None,
    config=None,
    generate: Callable[[object, str], str] | None = None,
    allowed_prices: set[int] | None = None,
) -> AiReply:
    """Minta AI menulis balasan; selalu aman dipakai (tidak pernah melempar).

    ``allowed_prices``: bila diberikan, setiap angka >= 1000 di jawaban harus ada
    di kumpulan itu (dipakai saat harga wajib dari daftar).
    """
    if config is None:
        return AiReply("", False, "AI tidak aktif")
    if not str(getattr(config, "base_url", "")).startswith(
        ("http://127.0.0.1", "http://localhost")
    ):
        return AiReply("", False, "Ollama bukan localhost")

    caller = generate or _default_generate
    try:
        raw = caller(
            config,
            build_prompt(incoming=incoming, context=context, entries=entries),
        )
    except Exception as exc:  # noqa: BLE001 - AI tidak boleh menggagalkan worker
        return AiReply("", False, f"Ollama gagal: {exc.__class__.__name__}")

    body = _clean(_parse(raw))
    if not body:
        return AiReply("", False, "balasan AI tidak terbaca")
    if len(body) < MIN_BODY_CHARS:
        return AiReply("", False, "balasan AI terlalu pendek")
    if len(body) > MAX_BODY_CHARS:
        # Potong di batas kata, bukan di tengah kata.
        body = body[:MAX_BODY_CHARS].rsplit(" ", 1)[0]

    banned = contains_forbidden_claim(body)
    extra = [word for word in _EXTRA_BANNED if word in body.lower()]
    if banned or extra:
        return AiReply("", False, f"klaim terlarang: {', '.join(banned + extra)}")

    if WHATSAPP_TEXT in body and WHATSAPP_TEXT not in body:
        return AiReply("", False, "nomor WA berubah")
    if LOCATION_TEXT in body and LOCATION_TEXT not in body:
        return AiReply("", False, "lokasi berubah")

    if allowed_prices is not None:
        luar = _unknown_prices(body, allowed_prices)
        if luar:
            return AiReply("", False, f"harga di luar daftar: {', '.join(luar)}")

    return AiReply(wrap(body), True, "balasan AI", raw_body=body)


def _unknown_prices(text: str, allowed: set[int]) -> list[str]:
    """Angka ribuan/rupiah di teks yang tidak ada di daftar harga."""
    luar = []
    for raw in re.findall(r"\d[\d.,]*", text or ""):
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 4:  # 64, 128, 256 = storage, bukan harga
            continue
        value = int(digits)
        if value not in allowed:
            luar.append(raw)
    return luar
