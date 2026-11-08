"""Uji notifikasi eskalasi (format pesan WA & normalisasi nomor)."""

from __future__ import annotations

from fbworkspace.notify import (
    HEADER,
    STOP_HEADER,
    Escalation,
    MemorySender,
    WhatsAppWebSender,
    _normalize_phone,
    build_login_escalation,
)


class TestEscalationText:
    def test_header_comes_first(self):
        text = Escalation(
            chat_name="Rian S.",
            message="bisa kurang ga kak?",
            reason="nego",
            url="https://www.facebook.com/messages/t/123",
            when="06/11 21:04",
        ).text()
        lines = text.splitlines()
        assert lines[0] == HEADER
        assert HEADER == "cek facebook ada chat yang bukan ranah saya"
        assert "Rian S." in text
        assert "bisa kurang ga kak?" in text
        assert "nego" in text
        assert "https://www.facebook.com/messages/t/123" in text

    def test_minimal_escalation(self):
        text = Escalation(chat_name="", message="", reason="di luar template").text()
        assert text.startswith(HEADER)
        assert "di luar template" in text

    def test_long_message_truncated(self):
        text = Escalation(chat_name="X", message="a" * 500, reason="r").text()
        assert len(text) <= 900
        assert "…" in text

    def test_login_escalation(self):
        escalation = build_login_escalation()
        assert escalation.header == STOP_HEADER
        assert "login" in escalation.text()


class TestPhone:
    def test_local_number_becomes_62(self):
        assert _normalize_phone("081513839973") == "6281513839973"

    def test_already_international(self):
        assert _normalize_phone("6281513839973") == "6281513839973"

    def test_without_leading_zero(self):
        assert _normalize_phone("81513839973") == "6281513839973"

    def test_with_symbols(self):
        assert _normalize_phone("+62 815-1383-9973") == "6281513839973"

    def test_empty(self):
        assert _normalize_phone("") == ""


class TestSender:
    def test_url_contains_target_and_text(self):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        url = sender.url_for("cek facebook")
        assert "web.whatsapp.com/send" in url
        assert "phone=6281513839973" in url
        assert "cek%20facebook" in url

    def test_memory_sender_records(self):
        sender = MemorySender()
        ok, _ = sender.send("halo")
        assert ok is True
        assert sender.sent == ["halo"]

    def test_memory_sender_failure(self):
        sender = MemorySender(ok=False, message="QR belum discan")
        ok, message = sender.send("halo")
        assert ok is False
        assert sender.sent == []
        assert "QR" in message
