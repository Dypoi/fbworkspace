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
import threading
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

    def send(self, text: str, *, blocking: bool = True) -> tuple[bool, str]:
        """Kirim teks lewat WhatsApp Web.

        ``blocking=False`` berarti "kirim di latar belakang": yang dilaporkan hanya
        apakah pengiriman berhasil DIMULAI, bukan hasil akhirnya. Dipakai dashboard
        supaya tombol tes tidak membekukan halaman 1 menit.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            return False, f"Playwright tidak tersedia: {exc.__class__.__name__}"

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if not blocking:
            thread = threading.Thread(
                target=self._send_blocking, args=(sync_playwright, text), daemon=True
            )
            thread.start()
            return True, (
                "pengiriman dimulai di latar belakang — cek WhatsApp Anda beberapa detik lagi; "
                "bila tidak masuk, jalankan: python -m fbworkspace.notify --test"
            )
        return self._send_blocking(sync_playwright, text)

    def _send_blocking(self, sync_playwright, text: str) -> tuple[bool, str]:
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        self.url_for(text),
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    # Beri waktu WhatsApp Web memuat sesi sebelum menyimpulkan.
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(20_000, self.timeout_ms))
                    except Exception:  # noqa: BLE001
                        pass
                    if page.query_selector(_WA_QR):
                        return False, "WhatsApp Web belum login (perlu scan QR lewat scripts/wa_login.py)"

                    composer = None
                    for selector in _WA_COMPOSER.split(","):
                        selector = selector.strip()
                        if not selector:
                            continue
                        try:
                            composer = page.wait_for_selector(selector, timeout=8_000)
                        except Exception:  # noqa: BLE001
                            composer = None
                        if composer is not None:
                            break
                    if composer is None:
                        detail = self._diagnose(page)
                        return False, f"kotak tulis WhatsApp tidak ditemukan ({detail})"

                    composer.click()
                    page.keyboard.type(text, delay=12)
                    page.wait_for_timeout(600)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3000)

                    needle = " ".join(text.split())[:30].lower()
                    body = " ".join(page.inner_text("body").split()).lower()
                    if needle and needle in body:
                        return True, "terkirim (terlihat di halaman)"
                    # Pesan sudah keluar dari kotak tulis = biasanya terkirim.
                    try:
                        still_there = composer.inner_text() or ""
                    except Exception:  # noqa: BLE001
                        still_there = ""
                    if needle and needle in " ".join(still_there.split()).lower():
                        return False, "pesan masih di kotak tulis (belum terkirim)"
                    return True, "terkirim (kotak tulis kosong)"
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            return False, _friendly_wa_error(exc)

    @staticmethod
    def _diagnose(page) -> str:
        """Ringkasan keadaan halaman WhatsApp untuk laporan kegagalan."""
        parts = []
        try:
            parts.append(f"url={page.url[:100]}")
        except Exception:  # noqa: BLE001
            pass
        try:
            parts.append(f"judul={page.title()[:60]!r}")
        except Exception:  # noqa: BLE001
            pass
        try:
            body = " ".join(page.inner_text("body").split())[:200]
            parts.append(f"teks={body!r}")
        except Exception:  # noqa: BLE001
            pass
        return " | ".join(parts)


def _friendly_wa_error(exc: Exception) -> str:
    """Terjemahkan error Playwright jadi langkah konkret untuk WhatsApp."""
    raw = f"{exc.__class__.__name__}: {str(exc)[:200]}"
    lowered = raw.lower()
    if "executable doesn't exist" in lowered or "looks like playwright" in lowered:
        return "Chromium Playwright belum terpasang. Jalankan: playwright install chromium"
    if "profile" in lowered and ("in use" in lowered or "locked" in lowered):
        return (
            "Profil WhatsApp sedang dipakai jendela lain. Tutup jendela WhatsApp Web "
            "milik aplikasi ini (atau jendela wa_login.py yang masih terbuka), lalu ulangi."
        )
    if "timeout" in lowered:
        return f"WhatsApp Web tidak selesai dimuat dalam batas waktu ({raw[:120]})"
    return raw


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


def main(argv: list[str] | None = None) -> int:
    """Periksa pengiriman WhatsApp tanpa membuka dashboard.

        python -m fbworkspace.notify --test
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Tes notifikasi WhatsApp (tidak lewat dashboard)")
    parser.add_argument("--test", action="store_true", help="Kirim satu pesan uji")
    parser.add_argument("--show", action="store_true", help="Tampilkan jendela browser")
    parser.add_argument("--text", default="tes notifikasi fbworkspace")
    args = parser.parse_args(argv)

    from .config import get_settings

    settings = get_settings()
    sender = WhatsAppWebSender(
        profile_dir=settings.wa_profile_dir,
        phone=settings.wa_phone,
        headless=not args.show,
    )
    print(f"Tujuan : {sender.phone}")
    print(f"Profil : {sender.profile_dir}")
    ok, note = sender.send(args.text, blocking=True)
    print(json.dumps({"ok": ok, "keterangan": note}, ensure_ascii=False, indent=2))
    if not ok:
        print(
            "\nPerbaikan yang paling sering berhasil:\n"
            "  1. .\\.venv\\Scripts\\python.exe scripts\\wa_login.py --show  (scan QR)\n"
            "  2. pastikan tidak ada jendela WhatsApp Web lain yang terbuka\n"
            "  3. ulangi perintah ini"
        )
    return 0 if ok else 1


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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
