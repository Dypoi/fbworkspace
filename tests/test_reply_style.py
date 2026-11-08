"""Uji variasi AI: gaya boleh berubah, fakta tidak boleh."""

from __future__ import annotations

import json

from fbworkspace.ollama_client import OllamaConfig
from fbworkspace.reply_style import build_prompt, polish

CONFIG = OllamaConfig(base_url="http://127.0.0.1:11434", model="qwen2.5:3b", timeout=5)


def _reply(text: str):
    """Peniru Ollama yang selalu menjawab dengan teks tertentu."""
    return lambda config, prompt: json.dumps({"balasan": text}, ensure_ascii=False)


class TestPrompt:
    def test_prompt_contains_rules_and_body(self):
        prompt = build_prompt("2.600.000 kak")
        assert "Jangan menambah, mengurangi, atau mengubah angka" in prompt
        assert "2.600.000 kak" in prompt
        assert "nama toko" in prompt


class TestAccepted:
    def test_style_change_accepted(self):
        result = polish("2.600.000 kak", config=CONFIG, generate=_reply("2.600.000 ya kak, siap"))
        assert result.changed is True
        assert "2.600.000" in result.text

    def test_location_kept(self):
        result = polish(
            "Kutabumi, Tangerang",
            config=CONFIG,
            generate=_reply("Lokasi saya di Kutabumi, Tangerang ya kak"),
        )
        assert result.changed is True
        assert "Kutabumi, Tangerang" in result.text

    def test_whatsapp_number_kept(self):
        result = polish(
            "081513839973", config=CONFIG, generate=_reply("Bisa WA ke 081513839973 kak")
        )
        assert result.changed is True

    def test_markdown_and_quotes_stripped(self):
        result = polish("Ready kak", config=CONFIG, generate=_reply('**"Ready kak, silakan"**'))
        assert result.changed is True
        assert "*" not in result.text
        assert '"' not in result.text
        assert result.text == "Ready kak, silakan"

    def test_markup_only_change_counts_as_no_change(self):
        result = polish("Ready kak", config=CONFIG, generate=_reply('**"Ready kak"**'))
        assert result.changed is False
        assert result.text == "Ready kak"

    def test_fenced_json_accepted(self):
        raw = '```json\n{"balasan": "Ready kak, bisa langsung"}\n```'
        result = polish("Ready kak", config=CONFIG, generate=lambda c, p: raw)
        assert result.changed is True

    def test_alternative_keys(self):
        result = polish(
            "Ready kak", config=CONFIG, generate=lambda c, p: json.dumps({"reply": "Ready kak ya"})
        )
        assert result.changed is True


class TestRejected:
    def test_changed_price_rejected(self):
        result = polish("2.600.000 kak", config=CONFIG, generate=_reply("2.500.000 kak"))
        assert result.changed is False
        assert result.text == "2.600.000 kak"
        assert "angka" in result.reason

    def test_extra_number_rejected(self):
        result = polish("2.600.000 kak", config=CONFIG, generate=_reply("2.600.000 kak, garansi 1 tahun"))
        assert result.changed is False

    def test_missing_whatsapp_rejected(self):
        result = polish(
            "081513839973", config=CONFIG, generate=_reply("Bisa chat saya ya kak")
        )
        assert result.changed is False
        assert "WA" in result.reason

    def test_missing_location_rejected(self):
        result = polish(
            "Kutabumi, Tangerang", config=CONFIG, generate=_reply("Bisa ketemuan kak")
        )
        assert result.changed is False

    def test_forbidden_claim_rejected(self):
        result = polish(
            "Ready kak", config=CONFIG, generate=_reply("Ready kak, garansi resmi sebulan")
        )
        assert result.changed is False
        assert "klaim" in result.reason

    def test_too_long_rejected(self):
        result = polish("Ready kak", config=CONFIG, generate=_reply("Ready kak " + "a" * 500))
        assert result.changed is False
        assert "panjang" in result.reason

    def test_too_short_rejected(self):
        result = polish("Ready kak", config=CONFIG, generate=_reply("ok"))
        assert result.changed is False

    def test_unreadable_answer_rejected(self):
        result = polish("Ready kak", config=CONFIG, generate=lambda c, p: "bukan json")
        assert result.changed is False
        assert result.text == "Ready kak"

    def test_no_change_keeps_original(self):
        result = polish("Ready kak", config=CONFIG, generate=_reply("Ready kak"))
        assert result.changed is False

    def test_ollama_down_keeps_original(self):
        def boom(config, prompt):
            raise OSError("connection refused")

        result = polish("Ready kak", config=CONFIG, generate=boom)
        assert result.changed is False
        assert result.text == "Ready kak"
        assert "Ollama gagal" in result.reason

    def test_unexpected_error_keeps_original(self):
        def boom(config, prompt):
            raise RuntimeError("aneh")

        result = polish("Ready kak", config=CONFIG, generate=boom)
        assert result.changed is False
        assert result.text == "Ready kak"


class TestDisabled:
    def test_no_config_keeps_original(self):
        result = polish("Ready kak", config=None)
        assert result.changed is False
        assert result.text == "Ready kak"

    def test_remote_endpoint_refused(self):
        remote = OllamaConfig(base_url="http://contoh.com:11434", model="qwen2.5:3b")
        result = polish("Ready kak", config=remote, generate=_reply("Ready kak ya"))
        assert result.changed is False
        assert "localhost" in result.reason

    def test_empty_body(self):
        result = polish("", config=CONFIG)
        assert result.changed is False
