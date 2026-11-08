"""Variasi kalimat balasan dengan Ollama — tanpa mengubah fakta.

Ide dasarnya sederhana dan sengaja "pelit":

- Fakta (harga, lokasi, nomor WA) **sudah** ditentukan oleh
  :mod:`fbworkspace.replies`. Modul ini hanya boleh mengubah gaya bahasa.
- Setelah AI menjawab, hasilnya diverifikasi: semua angka harus sama, nomor WA
  dan lokasi tidak boleh hilang/berubah, tidak boleh ada klaim terlarang, dan
  panjangnya tidak melebihi batas. Kalau satu saja gagal → pakai teks asli.
- Kalau Ollama mati/lambat, balasan tetap terkirim dengan teks template.

Dengan begitu AI tidak pernah menjadi sumber harga atau janji apa pun.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .drafts import contains_forbidden_claim
from .normalize import collapse_ws
from .replies import LOCATION_TEXT, MAX_REPLY_CHARS, WHATSAPP_TEXT

MIN_CHARS = 8

_SYSTEM = (
    "Kamu penulis balasan chat jual-beli HP di Indonesia. Tugasmu HANYA mengubah "
    "gaya bahasa menjadi lebih ramah dan singkat, maksimal dua kalimat.\n"
    "ATURAN KERAS:\n"
    "- Jangan menambah, mengurangi, atau mengubah angka apa pun.\n"
    "- Jangan menambah harga, stok, garansi, janji, atau alamat baru.\n"
    "- Jangan menyebut nama toko, area lain, atau jam operasional.\n"
    "- Pertahankan nomor WhatsApp dan kata lokasi persis seperti aslinya.\n"
    "- Jangan memakai tanda kutip, emoji, atau format markdown.\n"
    'Balas hanya JSON: {"balasan": "..."}'
)

_NUMBER_RE = re.compile(r"\d[\d.,]*")
_QUOTES_AND_MARKUP_RE = re.compile(r"[\"'`*_#\[\]{}<>|]")


@dataclass(frozen=True)
class StyleResult:
    """Hasil satu percobaan variasi."""

    text: str
    changed: bool
    reason: str = ""


def _numbers(text: str) -> list[str]:
    """Ambil potongan angka (harga, storage, nomor WA) untuk dibandingkan."""
    found = []
    for raw in _NUMBER_RE.findall(text or ""):
        digits = re.sub(r"\D", "", raw)
        if digits:
            found.append(digits.lstrip("0") or "0")
    return sorted(found)


def build_prompt(body: str) -> str:
    return f"{_SYSTEM}\n<<<BALASAN>>>\n{body}\n<<<END>>>\nJSON:"


def _validate(original: str, candidate: str) -> StyleResult:
    text = collapse_ws((candidate or "").replace("\n", " ")).strip()
    text = _QUOTES_AND_MARKUP_RE.sub("", text).strip()
    if len(text) < MIN_CHARS:
        return StyleResult(original, False, "jawaban AI terlalu pendek")
    if len(text) > MAX_REPLY_CHARS:
        return StyleResult(original, False, "jawaban AI terlalu panjang")
    if WHATSAPP_TEXT in original and WHATSAPP_TEXT not in text:
        return StyleResult(original, False, "nomor WA hilang")
    if LOCATION_TEXT in original and LOCATION_TEXT not in text:
        return StyleResult(original, False, "lokasi hilang")
    if _numbers(text) != _numbers(original):
        return StyleResult(original, False, "angka berubah")
    if contains_forbidden_claim(text):
        return StyleResult(original, False, "ada klaim terlarang")
    if text == original.strip():
        return StyleResult(original, False, "tidak ada perubahan")
    return StyleResult(text, True, "variasi AI")


def _default_generate(config, prompt: str) -> str:
    payload = {
        "model": config.model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": bool(getattr(config, "think", False)),
        # Suhu kecil: variasi tipis, bukan mengarang.
        "options": {"temperature": 0.4, "num_predict": 120, "num_ctx": 2048},
    }
    request = urllib.request.Request(
        f"{config.base_url.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(config.timeout)) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("response", ""))


def _parse(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(data, dict):
        for key in ("balasan", "reply", "text", "jawaban"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def polish(
    body: str,
    *,
    config=None,
    generate: Callable[[object, str], str] | None = None,
) -> StyleResult:
    """Coba variasikan ``body``; selalu kembalikan teks yang aman dikirim."""
    original = (body or "").strip()
    if not original or config is None:
        return StyleResult(original, False, "variasi AI tidak aktif")
    if not str(getattr(config, "base_url", "")).startswith(
        ("http://127.0.0.1", "http://localhost")
    ):
        return StyleResult(original, False, "Ollama bukan localhost")

    caller = generate or _default_generate
    try:
        raw = caller(config, build_prompt(original))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return StyleResult(original, False, f"Ollama gagal: {exc.__class__.__name__}")
    except Exception as exc:  # noqa: BLE001 - AI tidak boleh menggagalkan balasan
        return StyleResult(original, False, f"Ollama gagal: {exc.__class__.__name__}")

    candidate = _parse(raw)
    if not candidate:
        return StyleResult(original, False, "balasan AI tidak terbaca")
    return _validate(original, candidate)
