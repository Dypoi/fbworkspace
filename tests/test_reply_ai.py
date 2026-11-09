"""Uji balasan yang ditulis AI: penanda tetap, fakta dijaga, gagal = aman."""

from __future__ import annotations

import pytest

from fbworkspace.pricelist import default_entries
from fbworkspace.reply_ai import (
    CLOSING,
    OPENING,
    build_prompt,
    generate_reply,
    wrap,
)


class _Config:
    base_url = "http://127.0.0.1:11434"
    model = "qwen2.5:3b"
    timeout = 5
    think = False


def _gen(body: str):
    def caller(config, prompt):
        return '{"balasan": "%s"}' % body

    return caller


def test_wrap_always_adds_prefix_and_suffix():
    text = wrap("Ready kak, unit masih ada")
    assert text.startswith(OPENING)
    assert text.endswith(CLOSING)
    assert "Ready kak" in text


def test_wrap_does_not_double_when_model_already_wrote_them():
    text = wrap("hai Dypoi ready kak terimakasih dypoi")
    assert text.lower().count("hai dypoi") == 1
    assert text.lower().count("terimakasih dypoi") == 1


def test_prompt_contains_prices_locked_to_list():
    prompt = build_prompt(incoming="iphone 11 berapa?", entries=default_entries())
    assert "2.600.000" in prompt
    assert "Kutabumi, Tangerang" in prompt
    assert "081513839973" in prompt


def test_reply_is_wrapped_and_marked_ok():
    result = generate_reply(
        incoming="Iphone 11 ready?",
        entries=default_entries(),
        config=_Config(),
        generate=_gen("Ready kak, Iphone 11 masih ada."),
    )
    assert result.ok is True
    assert result.text.startswith(OPENING)
    assert result.text.endswith(CLOSING)


def test_forbidden_claim_rejected():
    result = generate_reply(
        incoming="ready?",
        config=_Config(),
        generate=_gen("Ready kak, bergaransi resmi setahun."),
    )
    assert result.ok is False
    assert "klaim" in result.reason


def test_prices_outside_list_rejected_when_guarded():
    result = generate_reply(
        incoming="iphone 11 berapa?",
        entries=default_entries(),
        config=_Config(),
        generate=_gen("Iphone 11 cuma 2.500.000 kak, murah."),
        allowed_prices={1_700_000, 2_000_000, 1_100_000, 1_300_000, 2_100_000,
                        2_300_000, 2_400_000, 2_600_000, 2_800_000},
    )
    assert result.ok is False
    assert "di luar daftar" in result.reason


def test_price_from_list_passes_guard():
    result = generate_reply(
        incoming="iphone 11 128 berapa?",
        entries=default_entries(),
        config=_Config(),
        generate=_gen("Iphone 11 128GB 2.600.000 kak."),
        allowed_prices={2_600_000},
    )
    assert result.ok is True


def test_model_failure_returns_not_ok():
    def boom(config, prompt):
        raise TimeoutError("ollama lambat")

    result = generate_reply(incoming="halo", config=_Config(), generate=boom)
    assert result.ok is False
    assert "TimeoutError" in result.reason


def test_empty_answer_is_not_ok():
    result = generate_reply(incoming="halo", config=_Config(), generate=_gen(""))
    assert result.ok is False


@pytest.mark.parametrize("body", ["ok", ""])
def test_too_short_or_blank_rejected(body):
    result = generate_reply(incoming="halo", config=_Config(), generate=_gen(body))
    assert result.ok is False


def test_no_config_means_no_ai():
    result = generate_reply(incoming="halo", config=None, generate=_gen("halo kak"))
    assert result.ok is False
