"""Uji deteksi status halaman (login/challenge/penolakan) dengan teks contoh.

Catatan: ini menguji LOGIKA deteksi, bukan halaman Facebook sungguhan.
Halaman nyata harus diuji lewat POC.
"""

from __future__ import annotations

import pytest

from fbworkspace.browser.page_state import detect_page_state
from fbworkspace.models import AccessStatus


class TestDetection:
    def test_normal_group_page(self):
        state = detect_page_state(
            "https://www.facebook.com/groups/123/",
            "Grup Jual Beli iPhone\nDijual iPhone 13 Pro Max 12jt",
        )
        assert state.status == AccessStatus.OK
        assert not state.is_blocking

    @pytest.mark.parametrize(
        "url,text",
        [
            ("https://www.facebook.com/login/?next=x", ""),
            ("https://www.facebook.com/groups/1/", "Masuk ke Facebook"),
            ("https://www.facebook.com/groups/1/", "You must log in to continue"),
        ],
    )
    def test_login_required(self, url, text):
        assert detect_page_state(url, text).status == AccessStatus.LOGIN_REQUIRED

    @pytest.mark.parametrize(
        "url,text",
        [
            ("https://www.facebook.com/checkpoint/123/", ""),
            ("https://www.facebook.com/groups/1/", "Please complete a security check"),
            ("https://www.facebook.com/groups/1/", "Kami mendeteksi aktivitas mencurigakan"),
            ("https://www.facebook.com/groups/1/", "Verifikasi identitas diperlukan"),
        ],
    )
    def test_checkpoint(self, url, text):
        assert detect_page_state(url, text).status == AccessStatus.CHECKPOINT

    @pytest.mark.parametrize(
        "text",
        [
            "You don't have permission to view this",
            "Hanya anggota yang dapat melihat konten ini",
            "This content isn't available right now",
            "Grup ini bersifat privat",
        ],
    )
    def test_permission_denied(self, text):
        assert detect_page_state("https://www.facebook.com/groups/1/", text).status == (
            AccessStatus.PERMISSION_DENIED
        )

    @pytest.mark.parametrize(
        "text",
        [
            "You're temporarily blocked",
            "Coba lagi nanti",
            "Too many requests",
        ],
    )
    def test_rate_limited(self, text):
        assert detect_page_state("https://www.facebook.com/groups/1/", text).status == (
            AccessStatus.RATE_LIMITED
        )

    def test_page_not_found(self):
        state = detect_page_state(
            "https://www.facebook.com/groups/1/", "This page isn't available"
        )
        assert state.status == AccessStatus.NOT_FOUND

    def test_priority_checkpoint_over_login(self):
        text = "Masuk ke Facebook. Please complete a security check"
        assert detect_page_state("https://www.facebook.com/groups/1/", text).status == (
            AccessStatus.CHECKPOINT
        )

    def test_empty_input_is_ok_not_error(self):
        # Tidak ada bukti pemblokiran -> jangan mengarang status blokir.
        assert detect_page_state("", "").status == AccessStatus.OK
