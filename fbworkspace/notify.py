"""Notifikasi eskalasi ke pemilik akun (WhatsApp Web + cadangan Windows).

Kapan dipakai: chat yang tidak bisa dijawab worker (nego, model di luar daftar,
pertanyaan lain) atau saat worker berhenti karena perlu login.

Aturan:
- Isi pesan WA selalu diawali kalimat tetap milik pemilik:
  "cek facebook ada chat yang bukan ranah saya".
- Tidak ada data sensitif lain selain nama chat, isi pesan, dan tautan chat.
- Kalau WhatsApp Web belum login / gagal, kegagalan dilaporkan jujur dan
  eskalasi tetap tersimpan di dashboard (tidak hilang).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from .normalize import truncate

HEADER = "cek facebook ada chat yang bukan ranah saya"
STOP_HEADER = "worker fbworkspace berhenti"
MAX_WA_CHARS = 900

# Selector kotak tulis WhatsApp Web (beberapa versi)
_WA_COMPOSER = (
    'div[contenteditable="true"][data-tab="10"], '
    'div[contenteditable="true"][data-tab="6"], '
    'div[contenteditable="true"][role="textbox"]'
)
_WA_QR = 'canvas[aria-label*="Scan"], canvas[aria-label*="scan"]'


@dataclass(frozen=True)
class Escalation:
    """Satu chat yang perlu perhatian pemilik."""

    chat_name: str
    message: str
    reason: str
    url: str = ""
    when: str = ""
    header: str = HEADER

    def text(self) -> str:
        lines = [self.header]
        if self.chat_name:
            lines.append(f"Chat  : {truncate(self.chat_name, 40)}")
        if self.when:
            lines.append(f"Waktu : {truncate(self.when, 30)}")
        if self.message:
            lines.append(f'Isi   : "{truncate(self.message, 220)}"')
        if self.reason:
            lines.append(f"Alasan: {truncate(self.reason, 120)}")
        if self.url:
            lines.append(f"Link  : {self.url}")
        return truncate("\n".join(lines), MAX_WA_CHARS)


class Sender(Protocol):
    name: str

    def send(self, text: str) -> tuple[bool, str]:
        """Kirim teks. Kembalikan (berhasil, keterangan)."""
        ...


@dataclass
class MemorySender:
    """Pengirim palsu untuk pengujian."""

    name: str = "memory"
    sent: list[str] = field(default_factory=list)
    ok: bool = True
    message: str = "terkirim"

    def send(self, text: str) -> tuple[bool, str]:
        if self.ok:
            self.sent.append(text)
        return self.ok, self.message


class WhatsAppWebSender:
    """Kirim pesan ke nomor Anda sendiri lewat WhatsApp Web (Playwright).

    Memakai profil browser terpisah supaya tidak bertabrakan dengan sesi
    Facebook. Perlu scan QR sekali lewat ``scripts/wa_login.py``.
    """

    name = "whatsapp-web"

    def __init__(
        self,
        profile_dir: str | Path,
        phone: str,
        *,
        headless: bool = True,
        timeout_ms: int = 60_000,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.phone = _normalize_phone(phone)
        self.headless = headless
        self.timeout_ms = timeout_ms

    def url_for(self, text: str) -> str:
        return f"https://web.whatsapp.com/send?phone={self.phone}&text={quote(text)}"

    def send(self, text: str) -> tuple[bool, str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            return False, f"Playwright tidak tersedia: {exc.__class__.__name__}"

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(self.url_for(text), wait_until="domcontentloaded", timeout=self.timeout_ms)
                    if page.query_selector(_WA_QR):
                        return False, "WhatsApp Web belum login (perlu scan QR)"
                    composer = page.wait_for_selector(_WA_COMPOSER, timeout=self.timeout_ms)
                    composer.click()
                    page.keyboard.type(text, delay=12)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2500)
                    return True, "terkirim"
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            return False, f"{exc.__class__.__name__}: {str(exc)[:200]}"


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0"):
        return "62" + digits[1:]
    if digits.startswith("8"):
        return "62" + digits
    return digits


def build_login_escalation(url: str = "") -> Escalation:
    return Escalation(
        chat_name="",
        message="Facebook meminta login/verifikasi, jadi balasan dihentikan.",
        reason="perlu login manual",
        url=url,
        header=STOP_HEADER,
    )


def windows_toast(title: str, message: str) -> tuple[bool, str]:
    """Cadangan: notifikasi Windows (best-effort, tidak wajib berhasil)."""
    if sys.platform != "win32":
        return False, "bukan Windows"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return False, "PowerShell tidak ditemukan"
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")[:250]
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(6000, '{safe_title}', '{safe_message}', 'Info');"
        "Start-Sleep -Seconds 1; $n.Dispose()"
    )
    try:
        subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
            check=False,
        )
        return True, "notifikasi Windows dikirim"
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"
