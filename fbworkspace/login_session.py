"""Buka browser untuk login manual sekali, lalu simpan sesi di profil.

Kenapa modul terpisah?
- Bisa dipakai sebagai CLI (`scripts/login_helper.py`) maupun dari dashboard.
- Status komunikasi ke dashboard lewat file marker kecil di `logs/`
  (bukan lewat stdout, supaya dashboard tidak perlu mem-parse log).

Keamanan:
- Jendela browser selalu TERLIHAT (tidak pernah headless).
- Kata sandi/OTP diisi sendiri oleh pengguna di jendela itu.
- Aplikasi hanya memeriksa ada/tidaknya cookie sesi `c_user`; nilainya tidak
  dibaca, tidak ditulis ke database, dan tidak dikirim ke mana pun.
- Profil browser (`data/browser_profile/`) masuk `.gitignore`.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Callable

from .browser.session import (
    LOGIN_STATUS_LABELS,
    LoginMarker,
    has_fb_session,
    read_login_marker,
    write_login_marker,
)
from .config import PROJECT_ROOT, get_settings

DEFAULT_TIMEOUT_SECONDS = 900
POLL_INTERVAL = 2.0


def default_marker_path() -> Path:
    return PROJECT_ROOT / "logs" / "login_session.json"


def _cookies(context) -> list[dict]:
    try:
        return list(context.cookies(["https://www.facebook.com", "https://www.facebook.com/"]))
    except Exception:  # noqa: BLE001 - jendela bisa ditutup pengguna
        try:
            return list(context.cookies())
        except Exception:  # noqa: BLE001
            return []


def _update(marker: LoginMarker, path: Path, status: str, message: str = "", error: str = "") -> None:
    marker.status = status
    marker.message = message or LOGIN_STATUS_LABELS.get(status, "")
    marker.error = error
    write_login_marker(path, marker)


def run_login_session(
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    marker_path: str | Path | None = None,
    on_progress: Callable[[str], None] | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> dict:
    """Buka Facebook di profil browser aplikasi dan tunggu login pengguna.

    Selesai bila: jendela ditutup pengguna, login terdeteksi lalu jendela
    ditutup, atau batas waktu habis. Tidak pernah mencoba menembus checkpoint.
    """
    settings = get_settings()
    settings.ensure_dirs()
    path = Path(marker_path) if marker_path else default_marker_path()

    marker = LoginMarker(
        status="menunggu",
        message="Membuka jendela browser…",
        profile_dir=str(settings.browser_profile_dir),
    )
    write_login_marker(path, marker)

    def progress(message: str) -> None:
        if on_progress:
            try:
                on_progress(message)
            except Exception:  # noqa: BLE001
                pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _update(marker, path, "error", error="Paket playwright belum terpasang.")
        return marker.as_dict()

    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    detected = False
    timed_out = False

    try:
        with sync_playwright() as pw:
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(settings.browser_profile_dir),
                    headless=False,  # wajib terlihat: pengguna harus bisa login
                    slow_mo=settings.browser_slow_mo_ms,
                    args=["--start-maximized"],
                    viewport=None,
                )
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "Executable doesn't exist" in text or "playwright install" in text:
                    friendly = (
                        "Chromium untuk Playwright belum terunduh. "
                        "Jalankan: playwright install chromium"
                    )
                elif "SingletonLock" in text or "ProcessSingleton" in text:
                    friendly = (
                        "Profil browser sedang dipakai proses lain. "
                        "Tutup semua jendela Chromium lalu coba lagi."
                    )
                else:
                    friendly = text[:300]
                _update(marker, path, "error", error=friendly)
                return marker.as_dict()

            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30_000)

            # Sudah pernah login? Tidak perlu menunggu.
            if has_fb_session(_cookies(context)):
                detected = True
                _update(
                    marker, path, "selesai",
                    "Sesi login sudah ada. Jendela ditutup otomatis.",
                )
                progress("Sesi login sudah ada.")
                page.wait_for_timeout(2500)
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                return marker.as_dict()

            _update(
                marker, path, "menunggu",
                "Login di jendela Chromium, lalu TUTUP jendela browser setelah feed tampil.",
            )
            progress("Menunggu login pengguna di jendela browser…")

            deadline = time.monotonic() + max(30, int(timeout_seconds))
            while True:
                try:
                    if not context.pages:
                        break  # jendela ditutup pengguna
                except Exception:  # noqa: BLE001
                    break

                if has_fb_session(_cookies(context)):
                    if not detected:
                        detected = True
                        _update(
                            marker, path, "login_berhasil",
                            "Login terdeteksi. Tutup jendela browser bila sudah selesai.",
                        )
                        marker.login_detected = True
                        progress("Login terdeteksi.")
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(poll_interval)

            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - jangan crash tanpa status
        _update(marker, path, "error", error=str(exc)[:300])
        return marker.as_dict()

    if detected:
        _update(
            marker, path, "selesai",
            "Sesi login tersimpan di profil browser. Jangan hapus data/browser_profile.",
        )
    elif timed_out:
        _update(marker, path, "timeout")
    else:
        _update(marker, path, "selesai_tanpa_login")
    return read_login_marker(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Buka browser FB Workspace untuk login Facebook sekali."
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Batas waktu menunggu login (detik, default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--marker", default=None,
        help="Lokasi file status (default logs/login_session.json)",
    )
    args = parser.parse_args(argv)

    print("=" * 68)
    print("Login manual FB Workspace")
    print("=" * 68)
    print("1. Jendela Chromium terbuka. Login sendiri di sana (termasuk OTP).")
    print("2. Setelah feed Facebook tampil, TUTUP jendela browser.")
    print("3. Kata sandi tidak pernah dibaca atau disimpan oleh aplikasi.")
    print("=" * 68)

    result = run_login_session(
        timeout_seconds=args.timeout,
        marker_path=args.marker,
        on_progress=lambda message: print(f"[login] {message}"),
    )
    status = result.get("status", "")
    print(f"\nStatus akhir: {status}")
    print(result.get("message") or LOGIN_STATUS_LABELS.get(status, ""))
    if result.get("error"):
        print(f"Detail: {result['error']}")
    if status == "selesai":
        print("\nLanjut: jalankan POC atau dashboard.")
        return 0
    if status == "login_berhasil":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
