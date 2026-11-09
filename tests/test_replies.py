"""Uji penyusun balasan DM: intent, harga dari daftar, dan aturan eskalasi."""

from __future__ import annotations

import pytest

from fbworkspace.pricelist import default_entries
from fbworkspace.replies import (
    ACTION_SEND,
    ACTION_SILENT,
    LOCATION_TEXT,
    WHATSAPP_TEXT,
    compose_reply,
    detect_intents,
    detect_model,
    detect_storage,
)

ENTRIES = default_entries()


def plan(message: str):
    return compose_reply(message, ENTRIES)


class TestDetection:
    @pytest.mark.parametrize(
        ("text", "key"),
        [
            ("iphone 11 ready?", "iphone 11"),
            ("ip 11 masih ada?", "iphone 11"),
            ("ip11 64gb", "iphone 11"),
            ("11 64gb ready", "iphone 11"),
            ("iphone xr harga", "iphone xr"),
            ("xr 128 berapa", "iphone xr"),
            ("iphone 8 berapa", "iphone 8"),
            ("iphone se gen2 64", "iphone se gen2"),
            ("se 2020 ready?", "iphone se gen2"),
        ],
    )
    def test_model(self, text, key):
        assert detect_model(text).model_key == key

    @pytest.mark.parametrize(
        ("text", "mention"),
        [
            ("iphone 14 pro harga?", "iphone 14 pro"),
            ("harga iphone 12", "iphone 12"),
        ],
    )
    def test_unknown_model_is_reported(self, text, mention):
        result = detect_model(text)
        assert result.model_key is None
        assert mention in (result.mention or "")

    @pytest.mark.parametrize(
        ("text", "storage"),
        [
            ("iphone 11 64gb", 64),
            ("iphone 11 128 gb nego", 128),
            ("xr 256gb", 256),
            ("iphone 11 128", 128),
            ("iphone 11 ready?", None),
        ],
    )
    def test_storage(self, text, storage):
        assert detect_storage(text) == storage

    def test_intents(self):
        assert "harga" in detect_intents("harga iphone 8 berapa")
        assert "ready" in detect_intents("masih ada kak?")
        assert "lokasi" in detect_intents("cod dimana")
        assert "wa" in detect_intents("minta no wa")
        assert "nego" in detect_intents("bisa nego ga")


class TestPrice:
    def test_exact_price_with_storage(self):
        result = plan("harga iphone 11 64gb?")
        assert result.action == ACTION_SEND
        assert "2.400.000" in result.body
        assert result.escalate is False

    def test_exact_price_from_bare_storage(self):
        assert "1.100.000" in plan("iphone 8 64gb berapa?").body

    def test_ambiguous_storage_asks_back(self):
        result = plan("harga iphone 11?")
        assert result.action == ACTION_SEND
        assert "Storage" in result.body
        assert "64GB" in result.body and "256GB" in result.body
        assert result.escalate is False

    def test_unknown_storage_escalates(self):
        result = plan("iphone 11 512gb harga?")
        assert result.action == ACTION_SILENT
        assert result.escalate is True
        assert "512" in result.reason

    def test_unknown_model_escalates(self):
        result = plan("harga iphone 14 pro")
        assert result.action == ACTION_SILENT
        assert result.escalate is True
        assert "14" in result.reason

    def test_price_without_model_escalates(self):
        result = plan("cek mahar")
        assert result.action == ACTION_SILENT
        assert result.escalate is True

    def test_short_model_and_storage_is_treated_as_price_question(self):
        assert "2.600.000" in plan("iphone 11 128").body


class TestReady:
    def test_ready_yes(self):
        assert plan("iphone 11 ready?").body == "Ready kak"

    def test_ready_no(self):
        from fbworkspace.pricelist import PriceEntry

        entries = [PriceEntry("iphone 11", "iPhone 11", 64, 2_400_000, ready=False)]
        result = compose_reply("iphone 11 ready?", entries)
        assert result.body == "Lagi kosong kak"

    def test_ready_with_unknown_storage_escalates(self):
        result = plan("iphone 11 512gb ready?")
        assert result.action == ACTION_SILENT
        assert result.escalate is True

    def test_ready_without_model(self):
        assert plan("masih ada kak?").body == "Ready kak"


class TestFixedTemplates:
    def test_location(self):
        assert plan("cod di mana?").body == LOCATION_TEXT
        assert LOCATION_TEXT == "Kutabumi, Tangerang"

    def test_whatsapp_number(self):
        assert plan("minta nomor wa").body == WHATSAPP_TEXT
        assert WHATSAPP_TEXT == "081513839973"

    def test_combined_answer(self):
        body = plan("11 64 ready? cod mana?").body
        assert "Ready kak" in body
        assert LOCATION_TEXT in body


class TestEscalation:
    @pytest.mark.parametrize(
        "text",
        [
            "bisa nego?",
            "kurangin dikit kak",
            "tukar tambah bisa?",
            "bisa kurang ga kak",
        ],
    )
    def test_nego_is_silent_and_escalated(self, text):
        result = plan(text)
        assert result.action == ACTION_SILENT
        assert result.escalate is True
        assert "nego" in result.reason

    def test_nego_with_price_still_notifies(self):
        result = plan("harga 11 64 nego")
        assert result.action == ACTION_SEND
        assert "2.400.000" in result.body
        assert result.escalate is True

    def test_out_of_template(self):
        result = plan("ini bisa dipakai buat trading ga?")
        assert result.action == ACTION_SILENT
        assert result.escalate is True
        assert result.reason == "di luar template"

    def test_acknowledgement_is_silent_but_greeting_gets_short_reply(self):
        for text in ("ok makasih", "sip", "p"):
            result = plan(text)
            assert result.action == ACTION_SILENT
            assert result.escalate is False
        greeting = plan("halo kak")
        assert greeting.action == ACTION_SEND
        assert greeting.body == "Hai kak, ada yang bisa dibantu?"
        assert greeting.escalate is False

    def test_buyer_phone_number_is_escalated(self):
        result = plan("wa saya 081234567890")
        assert result.action == ACTION_SILENT
        assert result.escalate is True
        assert "nomor" in result.reason

    def test_asking_for_our_number_with_their_number(self):
        result = plan("minta nomor wa 081234567890")
        assert result.body == WHATSAPP_TEXT
        assert result.escalate is True


class TestSafety:
    def test_never_invents_price(self):
        for text in ("harga iphone 12", "harga xr 512gb", "harga iphone se gen1"):
            result = plan(text)
            assert "Rp" not in result.body
            assert not any(char.isdigit() and "000" in result.body for char in "")

    def test_body_length_limited(self):
        result = plan("11 64 ready cod mana minta wa harga nego")
        assert len(result.body) <= 400

    def test_silent_has_no_body(self):
        assert plan("bisa nego?").body == ""


class TestGreetings:
    """Sapaan dibalas pendek; ucapan terima kasih tetap tidak perlu dibalas."""

    @pytest.mark.parametrize(
        "text",
        ["permisi kak", "halo kak", "assalamualaikum", "malam kak", "permisi", "Hai Dypoi"],
    )
    def test_greeting_gets_short_reply(self, text):
        plan = compose_reply(text, default_entries())
        assert plan.action == ACTION_SEND
        assert plan.body == "Hai kak, ada yang bisa dibantu?"
        assert plan.escalate is False

    def test_acknowledgement_stays_silent(self):
        plan = compose_reply("oke makasih", default_entries())
        assert plan.action == ACTION_SILENT
        assert plan.escalate is False

    def test_greeting_word_inside_real_question_is_not_an_acknowledgement(self):
        plan = compose_reply(
            "Hai dypoi, kalau jembut dimakan apakah berbahaya?", default_entries()
        )
        assert plan.action == ACTION_SILENT
        assert plan.escalate is True
        assert plan.reason == "di luar template"

    @pytest.mark.parametrize("text", ["berapa ya", "ini berapa", "harganya?"])
    def test_short_question_escalates(self, text):
        plan = compose_reply(text, default_entries())
        assert plan.action == "diam"
        assert plan.escalate is True


class TestContextFollowUp:
    """Pertanyaan lanjutan tanpa model: ambil konteks percakapan, fakta dari daftar."""

    def test_berapa_ya_uses_previous_model(self):
        plan = compose_reply("Berapa ya", default_entries(), context=["Iphone 11 ready?"])
        assert plan.action == ACTION_SEND
        assert "64GB" in plan.body and "256GB" in plan.body

    def test_berapa_ya_inherits_storage_from_context(self):
        plan = compose_reply("Berapa ya", default_entries(), context=["iphone 11 128gb ready?"])
        assert plan.action == ACTION_SEND
        assert "2.600.000" in plan.body

    def test_berapa_ya_without_context_escalates(self):
        plan = compose_reply("Berapa ya", default_entries(), context=[])
        assert plan.action == ACTION_SILENT
        assert plan.escalate is True

    def test_ambiguous_pending_models_asks_which_iphone(self):
        plan = compose_reply(
            "Berapa ya?",
            default_entries(),
            context=["Iphone 11 ready?"],
            ambiguous_models=["iPhone 8", "iPhone 11"],
        )
        assert plan.action == ACTION_SEND
        assert plan.body == "iPhone yang mana kak?"
        assert plan.escalate is False

    def test_context_does_not_override_other_model_in_message(self):
        plan = compose_reply("iphone 8 berapa", default_entries(), context=["iphone 11 ready?"])
        assert plan.action == ACTION_SEND
        assert "1.100.000" in plan.body or "64GB" in plan.body

    def test_unknown_model_context_still_escalates(self):
        plan = compose_reply("Berapa ya", default_entries(), context=["iphone 14 pro ready?"])
        assert plan.escalate is True


class TestLocationShorthand:
    @pytest.mark.parametrize("text", ["lok mna", "lm dmn", "cod dmn", "alamatnya dmn"])
    def test_location_shorthand(self, text):
        plan = compose_reply(text, default_entries())
        assert plan.action == ACTION_SEND
        assert plan.body == LOCATION_TEXT


class TestStorageOnlyReply:
    """Pembeli menjawab pertanyaan kita: "64gb" (tanpa menyebut model)."""

    CONTEXT = ["Iphone 11 ready?", "Storage-nya 64GB, 128GB, atau 256GB kak?"]

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("64gb", "2.400.000"),
            ("64", "2.400.000"),
            ("yang 64", "2.400.000"),
            ("64gb kak", "2.400.000"),
            ("128", "2.600.000"),
            ("256 gb", "2.800.000"),
        ],
    )
    def test_storage_only_uses_context(self, text, expected):
        plan = compose_reply(text, default_entries(), context=self.CONTEXT)
        assert plan.action == ACTION_SEND
        assert expected in plan.body

    def test_unknown_storage_escalates(self):
        plan = compose_reply("500gb", default_entries(), context=self.CONTEXT)
        assert plan.action == ACTION_SILENT
        assert plan.escalate is True

    def test_without_context_escalates(self):
        plan = compose_reply("64gb", default_entries(), context=[])
        assert plan.action == ACTION_SILENT
        assert plan.escalate is True

    @pytest.mark.parametrize("text", ["iphone 8 64gb", "xr 128gb berapa"])
    def test_model_in_message_still_wins(self, text):
        plan = compose_reply(text, default_entries(), context=["iphone 11 ready?"])
        assert plan.action == ACTION_SEND
        assert "11" not in plan.body  # harga mengikuti model yang disebut

    def test_nego_still_escalates(self):
        plan = compose_reply("64gb nego", default_entries(), context=self.CONTEXT)
        assert plan.escalate is True
