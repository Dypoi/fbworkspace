"""Deteksi sesi Facebook dan penantian login manual.

Modul ini sengaja dipisah dari Playwright supaya logikanya bisa diuji tanpa
membuka browser.

Prinsip:
- Login dilakukan OLEH PENGGUNA di jendela browser yang terlihat.
- Aplikasi hanya memeriksa keberadaan cookie sesi `c_user` (cookie standar
  Facebook yang menandakan ada sesi login) — aplikasi TIDAK membaca nilai
  cookie, tidak menyimpannya ke database, dan tidak pernah mengirimnya keluar.
- Tidak ada percobaan menembus checkpoint/CAPTCHA; penantian hanya memberi
  kesempatan pengguna menyelesaikannya sendiri.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

SESSION_COOKIE = "c_user"


def has_fb_session(cookies: Iterable[dict] | None) -> bool:
    """True bila ada cookie sesi Facebook yang menandakan login.

    Hanya nama cookie yang diperiksa; nilainya tidak pernah dipakai.
    """
    if not cookies:
        return False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if cookie.get("name") == SESSION_COOKIE and cookie.get("value"):
            return True
    return False


def cookie_names(cookies: Iterable[dict] | None) -> list[str]:
    """Daftar nama cookie (untuk diagnostik, tanpa nilai)."""
    names: list[str] = []
    for cookie in cookies or []:
        if isinstance(cookie, dict) and cookie.get("name"):
            names.append(str(cookie["name"]))
    return sorted(set(names))


@dataclass
class LoginWaitResult:
    detected: bool
    reason: str  # "login" | "timeout" | "stopped" | "disabled"

    @property
    def ok(self) -> bool:
        return self.detected

    def message(self) -> str:
        if self.detected:
            return "Login terdeteksi; melanjutkan pembacaan."
        if self.reason == "stopped":
            return "Proses dihentikan saat menunggu login."
        if self.reason == "disabled":
            return (
                "Facebook meminta login dan penantian login dimatikan "
                "(FBW_LOGIN_WAIT_SECONDS=0)."
            )
        return "Batas waktu menunggu login habis. Jalankan ulang setelah login."


def wait_for_login(
    *,
    cookie_provider: Callable[[], Iterable[dict] | None],
    timeout_seconds: int,
    should_stop: Callable[[], bool] | None = None,
    poll_interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_tick: Callable[[int], None] | None = None,
) -> LoginWaitResult:
    """Tunggu sampai cookie sesi muncul, waktu habis, atau diminta berhenti.

    Semua dependensi (jam, sleep, sumber cookie) disuntikkan agar mudah diuji.
    """
    if timeout_seconds <= 0:
        return LoginWaitResult(False, "disabled")

    deadline = clock() + float(timeout_seconds)
    while True:
        if should_stop is not None:
            try:
                if should_stop():
                    return LoginWaitResult(False, "stopped")
            except Exception:  # noqa: BLE001 - callback status tidak boleh mematikan proses
                pass

        try:
            if has_fb_session(cookie_provider()):
                return LoginWaitResult(True, "login")
        except Exception:  # noqa: BLE001 - browser bisa ditutup pengguna
            pass

        remaining = deadline - clock()
        if remaining <= 0:
            return LoginWaitResult(False, "timeout")
        if on_tick is not None:
            try:
                on_tick(int(remaining))
            except Exception:  # noqa: BLE001
                pass
        sleep(min(poll_interval, remaining))


# ---------------------------------------------------------------------------
# Marker file untuk komunikasi subprocess -> dashboard
# ---------------------------------------------------------------------------

LOGIN_STATUS_LABELS = {
    "menunggu": "Menunggu login di jendela browser…",
    "login_berhasil": "Login terdeteksi. Silakan tutup jendela browser setelah selesai.",
    "selesai": "Sesi login tersimpan. Profil browser siap dipakai.",
    "selesai_tanpa_login": "Jendela ditutup tanpa login terdeteksi.",
    "timeout": "Waktu tunggu habis tanpa login terdeteksi.",
    "error": "Terjadi kesalahan saat membuka browser.",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class LoginMarker:
    status: str = "menunggu"
    message: str = ""
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    login_detected: bool = False
    profile_dir: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message or LOGIN_STATUS_LABELS.get(self.status, ""),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "login_detected": self.login_detected,
            "profile_dir": self.profile_dir,
            "error": self.error,
        }


def write_login_marker(path: str | Path, marker: LoginMarker) -> None:
    """Tulis status ke file JSON kecil (aman untuk polling dashboard)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    marker.updated_at = _now_iso()
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(marker.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def read_login_marker(path: str | Path) -> dict:
    """Baca marker; kembalikan dict kosong bila belum ada/rusak."""
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def is_terminal_status(status: str | None) -> bool:
    return status in {"selesai", "selesai_tanpa_login", "timeout", "error"}
