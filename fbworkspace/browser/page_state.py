"""Deteksi keadaan halaman Facebook dari URL + teks yang terlihat.

Fungsi ini MURNI (tanpa jaringan) sehingga bisa diuji dengan teks contoh.
Tujuannya: berhenti dengan status jelas saat login/challenge/penolakan muncul,
bukan mencoba menembusnya.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import AccessStatus

# Urutan prioritas: makin atas makin penting untuk dihentikan.
_CHECKPOINT_MARKERS = (
    "security check",
    "checkpoint",
    "confirm your identity",
    "we've detected unusual activity",
    "we detected unusual activity",
    "mendeteksi aktivitas",
    "aktivitas mencurigakan",
    "aktivitas tidak biasa",
    "suspicious activity",
    "verify your identity",
    "verifikasi identitas",
    "pemeriksaan keamanan",
    "keamanan akun",
    "please complete a security check",
    "captcha",
    "kode keamanan",
    "two-factor",
    "verifikasi dua langkah",
)

_LOGIN_MARKERS = (
    "log in to facebook",
    "log into facebook",
    "masuk ke facebook",
    "masuk log masuk",
    "you must log in",
    "anda harus masuk",
    "create new account",
    "buat akun baru",
    "email or phone",
    "email atau nomor telepon",
)

_PERMISSION_MARKERS = (
    "you don't have permission",
    "you do not have permission",
    "anda tidak memiliki izin",
    "this content isn't available",
    "this content is not available",
    "konten ini tidak tersedia",
    "halaman ini tidak tersedia",
    "only members can see",
    "hanya anggota yang dapat melihat",
    "join this group to see",
    "gabung grup ini untuk melihat",
    "gabung grup untuk melihat",
    "this group is private",
    "grup ini bersifat privat",
    "grup ini privat",
    "content not found",
)

_RATE_LIMIT_MARKERS = (
    "temporarily blocked",
    "you're temporarily blocked",
    "we limit how often",
    "kami membatasi",
    "coba lagi nanti",
    "try again later",
    "too many requests",
    "terlalu banyak permintaan",
    "you have been temporarily restricted",
)

_NOT_FOUND_MARKERS = (
    "this page isn't available",
    "halaman ini tidak tersedia",
    "page not found",
    "link may be broken",
)


@dataclass
class PageState:
    status: AccessStatus
    detail: str
    markers: tuple[str, ...] = ()

    @property
    def is_blocking(self) -> bool:
        return self.status not in {AccessStatus.OK, AccessStatus.OK_EMPTY}


def _find_marker(lowered: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in lowered:
            return marker
    return None


def detect_page_state(
    url: str, visible_text: str, *, has_session: bool = False
) -> PageState:
    """Tebak status halaman dari URL dan teks yang tampak.

    Ini heuristik, bukan kepastian. Kalau ragu, hasilnya OK dan adapter tetap
    akan melaporkan jumlah postingan yang berhasil dibaca (bisa 0).

    `has_session=True` berarti cookie sesi Facebook (`c_user`) sudah ada.
    Dalam kasus itu penanda teks halaman login (mis. "create new account" yang
    bisa muncul di DOM walau sudah login) DIABAIKAN, supaya tidak salah
    menghentikan proses. URL /login atau /checkpoint tetap dihormati.
    """
    url_l = (url or "").lower()
    text_l = re.sub(r"\s+", " ", (visible_text or "").lower())

    if "/checkpoint" in url_l or "checkpoint" in url_l or "/recover" in url_l:
        return PageState(AccessStatus.CHECKPOINT, f"URL mengarah ke halaman checkpoint: {url_l[:120]}")
    if "/login" in url_l or "login.php" in url_l:
        return PageState(AccessStatus.LOGIN_REQUIRED, "URL mengarah ke halaman login.")
    if "captcha" in url_l:
        return PageState(AccessStatus.CHECKPOINT, "URL mengandung captcha.")

    marker = _find_marker(text_l, _CHECKPOINT_MARKERS)
    if marker:
        return PageState(
            AccessStatus.CHECKPOINT,
            f"Ditemukan penanda tantangan keamanan: '{marker}'.",
            (marker,),
        )

    if not has_session:
        marker = _find_marker(text_l, _LOGIN_MARKERS)
        if marker:
            return PageState(
                AccessStatus.LOGIN_REQUIRED,
                f"Ditemukan penanda halaman login: '{marker}'.",
                (marker,),
            )

    marker = _find_marker(text_l, _PERMISSION_MARKERS)
    if marker:
        return PageState(
            AccessStatus.PERMISSION_DENIED,
            f"Ditemukan penanda akses ditolak: '{marker}'.",
            (marker,),
        )

    marker = _find_marker(text_l, _RATE_LIMIT_MARKERS)
    if marker:
        return PageState(
            AccessStatus.RATE_LIMITED,
            f"Ditemukan penanda pembatasan sementara: '{marker}'.",
            (marker,),
        )

    marker = _find_marker(text_l, _NOT_FOUND_MARKERS)
    if marker:
        return PageState(
            AccessStatus.NOT_FOUND,
            f"Ditemukan penanda halaman tidak ada: '{marker}'.",
            (marker,),
        )

    return PageState(AccessStatus.OK, "Tidak ada penanda pemblokiran yang dikenali.")
