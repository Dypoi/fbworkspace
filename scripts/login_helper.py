"""Login manual sekali di profil browser yang dipakai FB Workspace.

    .\\.venv\\Scripts\\python.exe scripts\\login_helper.py

Jendela Chromium akan terbuka. Login sendiri di sana (termasuk OTP/checkpoint),
lalu tutup jendela browser setelah feed Facebook tampil. Sesi tersimpan di
`data/browser_profile/` dan dipakai oleh POC maupun dashboard.

Opsi:
    --timeout 900     batas waktu menunggu (detik)
    --marker PATH     lokasi file status (default logs/login_session.json)

Aplikasi tidak pernah membaca/menyimpan kata sandi Anda. Cookie sesi juga tidak
pernah diunggah ke mana pun.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fbworkspace.login_session import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
