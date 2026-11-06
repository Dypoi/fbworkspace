"""Proof of Concept (POC) pembacaan SATU grup Facebook.

Tujuan: membuktikan lebih dulu apakah isi grup bisa dibaca secara sah dan
apakah selector bekerja, SEBELUM memakai sistem penuh. Skrip ini:

- membuka SATU grup, dengan batas kecil (default 5 postingan / 90 detik),
- menggunakan jendela browser terlihat supaya Anda bisa login sendiri,
- berhenti dan melaporkan bila muncul login/checkpoint/penolakan,
- TIDAK menyimpan ke database, TIDAK mengirim apa pun ke Facebook,
- menyimpan hasil diagnostik ke logs/poc_*.json (masuk .gitignore),
- mencetak teks postingan yang berhasil dibaca agar Anda bisa menilai kualitasnya.

Cara pakai (Windows PowerShell, dari folder proyek):
    .\\.venv\\Scripts\\Activate.ps1
    python scripts\\poc_facebook.py --url "https://www.facebook.com/groups/123456789/"

Kode keluar:
    0 = berhasil membaca >= 1 postingan
    3 = halaman terbuka tetapi 0 postingan (selector/keanggotaan perlu diperiksa)
    4 = diblokir / perlu login / ada tantangan (proses dihentikan)
    5 = kesalahan teknis
    6 = dibatalkan pengguna
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fbworkspace.browser.base import CollectTarget  # noqa: E402
from fbworkspace.browser.playwright_fb import (  # noqa: E402
    DEFAULT_ARTICLE_SELECTOR,
    PlaywrightFacebookAdapter,
)
from fbworkspace.config import get_settings  # noqa: E402
from fbworkspace.models import (  # noqa: E402
    ACCESS_STATUS_MESSAGES,
    AccessStatus,
    CollectLimits,
)
from fbworkspace.normalize import validate_group_url  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="POC: cek apakah satu grup Facebook bisa dibaca dengan Playwright."
    )
    parser.add_argument("--url", required=True, help="Tautan grup Facebook")
    parser.add_argument("--max-posts", type=int, default=5, help="Maksimum postingan (default 5)")
    parser.add_argument("--max-seconds", type=int, default=90, help="Batas waktu detik (default 90)")
    parser.add_argument(
        "--selector",
        default=None,
        help=(
            "Selector kustom untuk elemen postingan. Biarkan kosong agar aplikasi "
            "memakai strategi bawaan: tautan /posts/, kartu feed, lalu "
            f"{DEFAULT_ARTICLE_SELECTOR} sebagai cadangan."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Jalankan tanpa jendela. TIDAK disarankan: Anda tidak bisa login sendiri.",
    )
    parser.add_argument(
        "--login-wait",
        type=int,
        default=None,
        help=(
            "Detik menunggu Anda login saat Facebook meminta login "
            "(default dari FBW_LOGIN_WAIT_SECONDS; 0 = langsung berhenti)."
        ),
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Lewati konfirmasi interaktif")
    return parser.parse_args(argv)


def confirm() -> bool:
    print("=" * 72)
    print("POC pembacaan grup Facebook (batas kecil)")
    print("=" * 72)
    print(
        "1. Jendela Chromium akan terbuka dan TERLIHAT. Kalau Facebook meminta login,\n"
        "   login sendiri di jendela itu. Skrip ini tidak menyimpan atau mengirim kredensial.\n"
        "2. Skrip berhenti total jika muncul CAPTCHA/checkpoint/penolakan.\n"
        "3. Tidak ada auto-comment, auto-post, atau auto-chat.\n"
        "4. Hasil dibaca dari halaman yang memang tampil untuk akun Anda. Kepatuhan pada\n"
        "   Ketentuan Facebook tetap tanggung jawab Anda.\n"
    )
    answer = input("Ketik 'lanjut' untuk memulai (atau Enter untuk batal): ").strip().lower()
    return answer == "lanjut"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    settings.ensure_dirs()

    validation = validate_group_url(args.url)
    if not validation.ok:
        print(f"[X] Tautan tidak valid: {validation.message}")
        return 5
    if validation.warning:
        print(f"[!] {validation.warning}")

    if not args.yes and not confirm():
        print("Dibatalkan.")
        return 6

    available, message = PlaywrightFacebookAdapter(
        profile_dir=settings.browser_profile_dir,
        headless=args.headless,
    ).is_available()
    print(f"[i] {message}")

    login_wait = settings.login_wait_seconds if args.login_wait is None else args.login_wait
    if login_wait > 0:
        print(
            f"[i] Jika Facebook meminta login, jendela dibiarkan terbuka sampai {login_wait} detik "
            "agar Anda bisa login sendiri."
        )

    limits = CollectLimits(
        max_posts=max(1, min(args.max_posts, settings.hard_max_posts)),
        max_seconds=max(15, min(args.max_seconds, 900)),
    )
    adapter = PlaywrightFacebookAdapter(
        profile_dir=settings.browser_profile_dir,
        headless=args.headless,
        slow_mo_ms=settings.browser_slow_mo_ms,
        # None = pakai strategi bawaan (tautan -> kartu feed -> selector cadangan).
        article_selector=args.selector or None,
        login_wait_seconds=login_wait,
    )

    def progress(level: str, msg: str, counters: dict) -> None:
        icon = {"info": "·", "warning": "!", "error": "X"}.get(level, "·")
        print(f"[{icon}] {msg}")

    target = CollectTarget(group_urls=[validation.normalized], limits=limits)
    outcome = adapter.collect(target, on_progress=progress)

    print()
    print("=" * 72)
    print(f"STATUS : {outcome.status.value}")
    print(f"PESAN  : {outcome.message or ACCESS_STATUS_MESSAGES.get(outcome.status, '')}")
    print(f"POSTINGAN TERBACA: {len(outcome.posts)}")
    print(f"METODE EKSTRAKSI : {outcome.diagnostics.get('extraction', 'tidak diketahui')}")
    if args.selector:
        print(f"SELECTOR KUSTOM  : {args.selector}")
    skipped = int(outcome.diagnostics.get("skipped_comment") or 0)
    empty_cards = int(outcome.diagnostics.get("empty_cards") or 0)
    if empty_cards:
        print(f"KARTU KOSONG      : {empty_cards} (placeholder/belum termuat)")
    if skipped:
        print(f"KOMENTAR DILEWATI : {skipped} (bukan isi postingan)")
        for sample in (outcome.diagnostics.get("sample_texts") or [])[:3]:
            print(f"  contoh: {sample[:120]}")
    print("=" * 72)

    for index, post in enumerate(outcome.posts, start=1):
        print(f"\n--- Postingan {index} ---")
        print(
            "URL    : "
            + (post.post_url or "(tidak tersedia — kemungkinan postingan anonim)")
        )
        print(f"Teks   : {post.text[:700]}{'…' if len(post.text) > 700 else ''}")

    diagnostics = dict(outcome.diagnostics)
    diagnostics["requested_url"] = validation.normalized
    diagnostics["limits"] = {"max_posts": limits.max_posts, "max_seconds": limits.max_seconds}
    diagnostics["status"] = outcome.status.value
    diagnostics["message"] = outcome.message
    diagnostics["posts_found"] = len(outcome.posts)
    diagnostics["timestamp"] = datetime.now().isoformat(timespec="seconds")

    out_path = settings.log_dir / f"poc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        out_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] Diagnostik disimpan di: {out_path} (folder logs/ tidak masuk Git)")
    except OSError as exc:
        print(f"[!] Gagal menyimpan diagnostik: {exc}")

    print("\nCara membaca hasil:")
    print("- status 'ok' + postingan > 0 : selector mungkin bekerja. Periksa apakah teksnya utuh.")
    print("- status 'ok' + 0 postingan   : halaman terbuka tapi tidak ada postingan terbaca.")
    print("  Kemungkinan: perlu gabung grup, konten belum dimuat, atau selector perlu diubah.")
    print("- 'diblokir'/'perlu_login'     : Facebook membatasi. Jangan dipaksa; pakai input manual.")

    if outcome.status in {AccessStatus.OK} and outcome.posts:
        return 0
    if outcome.status in {AccessStatus.OK_EMPTY}:
        return 3
    if outcome.status == AccessStatus.STOPPED:
        return 6
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
