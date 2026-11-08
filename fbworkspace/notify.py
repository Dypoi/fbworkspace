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

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from .normalize import truncate

HEADER = "cek facebook ada chat yang bukan ranah saya"
STOP_HEADER = "worker fbworkspace berhenti"
MAX_WA_CHARS = 900

# Selector kotak tulis WhatsApp Web (beberapa versi). Urutan penting: yang paling
# spesifik dulu, terakhir cadangan umum. Kotak tulis WhatsApp punya aria-label
# "Ketik pesan"/"Type a message" dan berada di footer.
_WA_COMPOSER = (
    'footer div[contenteditable="true"][role="textbox"], '
    'div[contenteditable="true"][data-tab="10"], '
    'div[contenteditable="true"][data-tab="6"], '
    'div[contenteditable="true"][aria-label*="Ketik pesan"], '
    'div[contenteditable="true"][aria-label*="Type a message"], '
    'div[contenteditable="true"][role="textbox"], '
    'div[contenteditable="true"][spellcheck="true"]'
)
_WA_QR = 'canvas[aria-label*="Scan"], canvas[aria-label*="scan"]'
# Halaman error Chrome yang muncul bila WhatsApp Web menolak versi Chrome.
_WA_BROWSER_BLOCK_WORDS = (
    "perbarui chrome",
    "whatsapp dapat digunakan di google chrome",
    "update chrome",
    "uses google chrome",
    "chrome 100+",
)


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

    def send(
        self, text: str, *, blocking: bool = True, attempt: int = 1
    ) -> tuple[bool, str]:
        """Kirim teks lewat WhatsApp Web.

        ``blocking=False`` berarti "kirim di latar belakang": yang dilaporkan hanya
        apakah pengiriman berhasil DIMULAI, bukan hasil akhirnya. Dipakai dashboard
        supaya tombol tes tidak membekukan halaman 1 menit.

        ``attempt`` > 1 dipakai worker untuk mengulang: WhatsApp Web kadang belum
        siap saat dijalankan tanpa jendela (headless), padahal berhasil bila
        jendela browser ditampilkan.
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
        return self._send_blocking(sync_playwright, text, attempt=attempt)

    def _send_blocking(self, sync_playwright, text: str, attempt: int = 1) -> tuple[bool, str]:
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

                    # WhatsApp kadang baru menyiapkan kotak tulis setelah sesi siap.
                    composer = None
                    for attempt in range(3):
                        composer = _find_wa_composer(page, timeout_ms=6_000)
                        if composer is not None:
                            break
                        page.wait_for_timeout(2_500)
                    if composer is None:
                        blocked = self._browser_blocked(page)
                        detail = self._diagnose(page)
                        if blocked:
                            return False, (
                                "WhatsApp Web menolak versi Chrome bawaan Playwright. "
                                "Jalankan sekali di PowerShell: "
                                "\\.\\venv\\Scripts\\python.exe -m fbworkspace.notify --setup-profile "
                                f"(lalu scan QR). Detail: {detail}"
                            )
                        return False, (
                            f"kotak tulis WhatsApp tidak ditemukan "
                            f"(percobaan {attempt}; {detail})"
                        )

                    # Pastikan kotak tulis benar-benar siap menerima ketikan.
                    try:
                        composer.click()
                        page.wait_for_timeout(400)
                    except Exception:  # noqa: BLE001
                        pass
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

    def inspect(self, *, headless: bool = True) -> tuple[bool, str]:
        """Buka WhatsApp Web dan laporkan keadaannya tanpa mengirim apa pun."""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            return False, f"Playwright tidak tersedia: {exc.__class__.__name__}"

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=headless
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        f"https://web.whatsapp.com/send?phone={self.phone}",
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(3_000)
                    qr = bool(page.query_selector(_WA_QR))
                    composer = _find_wa_composer(page, timeout_ms=8_000)
                    blocked = self._browser_blocked(page)
                    version = ""
                    try:
                        version = f"{p.chromium.name} {page.evaluate('navigator.userAgent')[:80]}"
                    except Exception:  # noqa: BLE001
                        version = ""
                    parts = [
                        f"qr={qr}",
                        f"kotak_tulis={'ada' if composer is not None else 'tidak ada'}",
                        f"ditolak_versi_chrome={blocked}",
                        self._diagnose(page),
                    ]
                    if version:
                        parts.append(f"browser={version}")
                    ok = composer is not None and not qr and not blocked
                    if not qr:
                        WhatsAppWebSender(
                            profile_dir=self.profile_dir, phone=self.phone
                        )._save_probe(headless_ok=ok, mode=f"periksa-{'headless' if headless else 'terlihat'}")
                    return ok, " | ".join(parts)
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            return False, _friendly_wa_error(exc)

    @staticmethod
    def _browser_blocked(page) -> bool:
        try:
            body = " ".join(page.inner_text("body").split()).lower()
        except Exception:  # noqa: BLE001
            return False
        return any(word in body for word in _WA_BROWSER_BLOCK_WORDS)

    # --------------------------------------------------------------- memori mode
    @property
    def _probe_path(self) -> Path:
        """Catatan hasil percobaan: apakah mode headless bisa dipakai di laptop ini."""
        return Path(self.profile_dir).parent / "wa_mode.json"

    def _load_probe(self) -> dict:
        try:
            return json.loads(self._probe_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - file belum ada / rusak: anggap belum tahu
            return {}

    def _save_probe(self, **values) -> None:
        data = self._load_probe()
        data.update(values)
        data["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self._probe_path.parent.mkdir(parents=True, exist_ok=True)
            self._probe_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def headless_works(self) -> bool:
        """Apakah WhatsApp Web bisa dibuka tanpa jendela di mesin ini.

        Diambil dari hasil percobaan sebelumnya (``--compare``/``--diagnose``/
        pengiriman yang gagal). Bila belum pernah dicoba, dianggap bisa supaya
        worker tidak membuka jendela tanpa alasan.
        """
        return bool(self._load_probe().get("headless_ok", True))

    def send_reliable(self, text: str, *, tries: int = 3) -> tuple[bool, str]:
        """Kirim dengan beberapa percobaan; headless dulu, lalu jendela terlihat.

        Worker memakai ini untuk notifikasi. Pengalaman nyata 8 Nov: perintah
        ``notify --test --show`` berhasil, sedangkan worker (headless) gagal.
        Percobaan pertama tetap headless (tidak mengganggu layar); bila gagal,
        percobaan berikutnya memakai jendela terlihat supaya sama seperti perintah
        yang terbukti berhasil.
        """
        notes: list[str] = []
        tries = max(1, int(tries))
        # Sudah terbukti gagal headless di laptop ini (mis. "perbarui Chrome"):
        # langsung pakai jendela terlihat supaya notifikasi tidak terlambat.
        start_visible = not self.headless_works() or not self.headless
        for attempt in range(1, tries + 1):
            visible = start_visible or attempt >= 2
            sender = (
                self
                if visible == (not self.headless)
                else WhatsAppWebSender(
                    profile_dir=self.profile_dir,
                    phone=self.phone,
                    headless=not visible,
                )
            )
            ok, note = sender.send(text, blocking=True, attempt=attempt)
            if ok:
                if attempt > 1:
                    note = f"{note} (berhasil pada percobaan {attempt})"
                if attempt >= 2 and not start_visible:
                    self._save_probe(headless_ok=False, reason=notes[0][:180])
                return True, note
            notes.append(f"coba {attempt}: {note}")
            if not sender.headless and attempt >= 2 and "kotak tulis" in note.lower():
                self._save_probe(headless_ok=False, reason=note[:180])
            if "belum login" in note.lower() or "scan qr" in note.lower():
                break  # percobaan ulang tidak akan menolong
        return False, " | ".join(notes)[:600]

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


def _find_wa_composer(page, *, timeout_ms: int = 6_000):
    """Cari kotak tulis WhatsApp; coba tiap selector lalu cadangan umum."""
    for selector in [part.strip() for part in _WA_COMPOSER.split(",") if part.strip()]:
        try:
            found = page.wait_for_selector(selector, timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            return found
    # Cadangan terakhir: contenteditable mana pun yang terlihat.
    try:
        for candidate in page.query_selector_all('div[contenteditable="true"]'):
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


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
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Buka WhatsApp Web dan laporkan apa yang terlihat (tidak mengirim)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help=(
            "Bandingkan mode worker (headless) vs jendela terlihat: buktikan mana yang "
            "bisa membuka WhatsApp Web di laptop ini (tidak mengirim apa pun)"
        ),
    )
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
    if args.compare:
        results = {}
        for label, use_headless in (("worker (headless)", True), ("terlihat (--show)", False)):
            probe = WhatsAppWebSender(
                profile_dir=settings.wa_profile_dir,
                phone=settings.wa_phone,
                headless=use_headless,
            )
            ok, note = probe.inspect(headless=use_headless)
            results[label] = {"ok": ok, "keterangan": note}
            print(f"{label}: {'OK' if ok else 'GAGAL'}")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        siap = results["terlihat (--show)"]["ok"]
        headless_ok = results["worker (headless)"]["ok"]
        if siap and not headless_ok:
            # Hindari "=false" di dalam teks print: sebagian konsol PowerShell
            # memotongnya sehingga tampak seperti baris .env yang salah.
            baris = "FBW_WA_HEADLESS" + chr(61) + "false"
            print("\nKesimpulan: WhatsApp Web bisa dipakai, tetapi TIDAK bisa dari mode")
            print("headless di laptop ini. Tambahkan baris berikut ke file .env:")
            print()
            print("    " + baris)
            print()
            print(
                "Worker juga mengingatnya sendiri (data/wa_mode.json), jadi pengaturan "
                "ini hanya perlu bila Anda ingin mematangkannya."
            )
        elif not siap:
            print(
                "\nKesimpulan: WhatsApp Web belum siap sama sekali. Jalankan "
                "scripts\\wa_login.py lalu scan QR."
            )
        else:
            print("\nKesimpulan: kedua mode bisa. Notifikasi worker seharusnya terkirim.")
        return 0 if siap else 1

    if args.diagnose:
        ok, note = sender.inspect(headless=not args.show)
        print(json.dumps({"ok": ok, "keterangan": note}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    ok, note = sender.send(args.text, blocking=True)
    print(json.dumps({"ok": ok, "keterangan": note}, ensure_ascii=False, indent=2))
    if not ok:
        print("\n" + remedy_for(note))
    return 0 if ok else 1


def remedy_for(note: str) -> str:
    """Saran perbaikan yang sesuai dengan sebab kegagalan (bukan daftar umum).

    Sebelumnya semua kegagalan disarankan "scan QR", padahal TargetClosedError
    berarti jendela/halaman tertutup — menyarankan scan QR hanya membingungkan.
    """
    text = str(note or "").lower()
    if "targetclosed" in text or "closed" in text or "tertutup" in text:
        return (
            "Sebab: jendela/halaman WhatsApp Web tertutup sebelum pesan terkirim.\n"
            "Perbaikan:\n"
            "  1. ulangi perintah ini (jendela akan dibuka lagi otomatis)\n"
            "  2. jangan menutup jendela Chromium yang muncul selama ±10-20 detik\n"
            "  3. tutup WhatsApp Web yang terbuka di browser biasa Anda "
            "(dua tab WA bisa saling menendang sesi)"
        )
    if "chrome" in text and ("versi" in text or "100+" in text):
        return (
            "Sebab: mode tanpa jendela (headless) ditolak WhatsApp Web di laptop ini.\n"
            "Perbaikan:\n"
            "  1. gunakan jendela terlihat: tambahkan --show, atau\n"
            "  2. tambahkan baris berikut ke file .env lalu restart dashboard:\n"
            "     FBW_WA_HEADLESS=false\n"
            "  3. bila masih gagal: .\\.venv\\Scripts\\python.exe -m fbworkspace.notify --compare"
        )
    if "qr" in text or "login" in text or "scan" in text:
        return (
            "Sebab: sesi WhatsApp Web belum aktif.\n"
            "Perbaikan:\n"
            "  1. .\\.venv\\Scripts\\python.exe scripts\\wa_login.py --show  (scan QR)\n"
            "  2. ulangi perintah ini"
        )
    return (
        "Perbaikan yang paling sering berhasil:\n"
        "  1. ulangi perintah ini sekali lagi\n"
        "  2. pastikan tidak ada jendela WhatsApp Web lain yang terbuka\n"
        "  3. .\\.venv\\Scripts\\python.exe -m fbworkspace.notify --compare "
        "(membandingkan mode jendela vs tanpa jendela)"
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
