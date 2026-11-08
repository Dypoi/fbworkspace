"""PIN pemulihan chat Messenger (mis. saat Facebook minta PIN untuk restore chat).

Kenapa modul terpisah:
- PIN adalah rahasia. Tidak boleh masuk log, status job, database, atau pesan WA.
- Mengisi PIN hanya boleh terjadi pada **satu** layar yang tepat: permintaan PIN
  untuk memulihkan riwayat chat. Bukan di halaman checkpoint, verifikasi identitas,
  kode 2FA, atau form kata sandi akun — di layar-layar itu kita **berhenti**.
- Kalau ragu, lebih baik berhenti dan memberi tahu pemilik daripada mengetik PIN
  ke tempat yang salah.

PIN dibaca dari `.env` (``FBW_CHAT_PIN``) yang diabaikan Git. Jangan pernah
menuliskan PIN ke kode sumber.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Layar yang MEMANG meminta PIN pemulihan chat.
PIN_PROMPT_KEYWORDS = (
    "masukkan pin",
    "masukan pin",
    "enter your pin",
    "pin pemulihan",
    "recovery pin",
    "pulihkan chat",
    "pulihkan riwayat chat",
    "restore your chat",
    "restore chat history",
    "secure storage",
    "penyimpanan aman",
    "kunci enkripsi",
    "end-to-end encrypted chat",
)

# Layar yang TIDAK boleh diisi PIN, apa pun isinya.
PIN_FORBIDDEN_KEYWORDS = (
    "checkpoint",
    "verifikasi identitas",
    "konfirmasi identitas",
    "verify your identity",
    "security check",
    "kode verifikasi",
    "verification code",
    "two-factor",
    "dua faktor",
    "kata sandi",
    "password",
    "akun anda telah dinonaktifkan",
    "your account has been locked",
)

# Selector kotak PIN (berlapis, urut prioritas).
PIN_INPUT_SELECTORS = (
    'input[inputmode="numeric"]',
    'input[name*="pin" i]',
    'input[aria-label*="PIN" i]',
    'input[type="password"]',
)

_DIGITS_RE = re.compile(r"^\d{4,10}$")

MAX_ATTEMPTS_PER_RUN = 2


@dataclass(frozen=True)
class PinDecision:
    """Hasil penilaian: boleh mengetik PIN atau tidak, beserta alasannya."""

    allowed: bool
    reason: str
    keyword: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - kenyamanan
        return self.allowed


def mask(pin: str) -> str:
    """Tampilan aman untuk log/UI: hanya panjangnya yang terlihat."""
    digits = re.sub(r"\D", "", pin or "")
    if not digits:
        return "(kosong)"
    return f"{'•' * len(digits)} ({len(digits)} angka)"


def is_valid_pin(pin: str) -> bool:
    return bool(_DIGITS_RE.match((pin or "").strip()))


def looks_like_pin_prompt(text: str) -> bool:
    lowered = " ".join((text or "").lower().split())
    return any(word in lowered for word in PIN_PROMPT_KEYWORDS)


def decide(
    *,
    page_text: str,
    url: str = "",
    title: str = "",
    pin: str,
    auto: bool = True,
) -> PinDecision:
    """Bolehkah worker mengisi PIN di layar ini sekarang?"""
    blob = " ".join(f"{url} {title} {page_text}".lower().split())

    forbidden = [word for word in PIN_FORBIDDEN_KEYWORDS if word in blob]
    if forbidden:
        return PinDecision(
            False,
            f"layar bukan permintaan PIN ({forbidden[0]}) — berhenti, jangan ketik PIN",
            forbidden[0],
        )

    if not pin:
        return PinDecision(False, "PIN belum diisi di .env (FBW_CHAT_PIN)")
    if not is_valid_pin(pin):
        return PinDecision(False, "FBW_CHAT_PIN harus 4–10 angka saja")
    if not auto:
        return PinDecision(False, "isi PIN otomatis dimatikan (FBW_CHAT_PIN_AUTO=false)")

    keyword = next((word for word in PIN_PROMPT_KEYWORDS if word in blob), "")
    if not keyword:
        return PinDecision(False, "bukan layar permintaan PIN")
    return PinDecision(True, "layar permintaan PIN terdeteksi", keyword)


def fill_pin(page, pin: str, *, timeout_ms: int = 10_000) -> tuple[bool, str]:
    """Ketik PIN ke kotak yang tersedia lalu Enter. Tidak pernah mengembalikan PIN."""
    if not is_valid_pin(pin):
        return False, "PIN tidak valid (harus 4–10 angka)"

    box = None
    for selector in PIN_INPUT_SELECTORS:
        try:
            box = page.query_selector(selector)
        except Exception:  # noqa: BLE001
            box = None
        if box is not None:
            break
    if box is None:
        return False, "kotak PIN tidak ditemukan di halaman"

    try:
        box.click()
        for char in pin:
            page.keyboard.type(char, delay=80)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
    except Exception as exc:  # noqa: BLE001
        return False, f"gagal mengisi PIN: {exc.__class__.__name__}"
    return True, "PIN diisi lalu Enter"


def pin_escalation_text() -> str:
    """Pesan untuk pemilik saat PIN dibutuhkan tetapi tidak boleh diisi otomatis."""
    return (
        "Messenger meminta PIN pemulihan chat dan worker tidak mengisinya otomatis.\n"
        "Buka facebook.com/messages di jendela browser worker, isi PIN sendiri, "
        "lalu jalankan ulang. Bila ingin otomatis, isi FBW_CHAT_PIN di .env."
    )
