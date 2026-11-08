"""Login WhatsApp Web sekali untuk notifikasi eskalasi.

Jalankan:

    .\\.venv\\Scripts\\python.exe scripts\\wa_login.py

Jendela browser akan terbuka di web.whatsapp.com. Scan QR dari ponsel Anda
(WhatsApp -> Perangkat tertaut). Setelah daftar chat muncul, sesi tersimpan di
folder profil (data/wa_profile) dan bisa dipakai worker tanpa scan lagi.

Kalau sesi kedaluwarsa, jalankan ulang skrip ini.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fbworkspace.config import get_settings, resolve_path  # noqa: E402

LOGIN_WAIT_SECONDS = 300


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Login WhatsApp Web (scan QR sekali)")
    parser.add_argument(
        "--profile",
        default="",
        help="Folder profil browser (default: FBW_WA_PROFILE_DIR / data/wa_profile)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    profile: Path = resolve_path(args.profile) if args.profile else settings.wa_profile_dir
    profile.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"Playwright belum terpasang: {exc}")
        print("Jalankan: pip install -r requirements.txt && playwright install chromium")
        return 2

    print(f"Profil WhatsApp : {profile}")
    print("Membuka web.whatsapp.com ... scan QR dari ponsel Anda.")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded")
            try:
                # #pane-side = daftar chat, hanya muncul setelah login berhasil
                page.wait_for_selector("#pane-side", timeout=LOGIN_WAIT_SECONDS * 1000)
                print("Login terdeteksi. Sesi tersimpan; jendela ditutup dalam 3 detik.")
                time.sleep(3)
                return 0
            except Exception:  # noqa: BLE001
                print(
                    "Belum terdeteksi login dalam batas waktu. Jalankan ulang skrip ini "
                    "dan pastikan QR sudah discan."
                )
                return 1
        finally:
            context.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
