"""Uji validasi keluaran model AI (tanpa memanggil model)."""

from __future__ import annotations

from fbworkspace.analyzer import validate_llm_payload
from fbworkspace.ollama_client import (
    OllamaConfig,
    build_prompt,
    is_local_endpoint,
    summarize_config,
)


def valid_payload() -> dict:
    return {
        "category": "menjual",
        "iphone_model": "iPhone 13 Pro Max",
        "price_amount": 12500000,
        "price_kind": "harga",
        "location": "Tangerang",
        "details": {"storage": "256 GB", "battery_health": "89%"},
        "relevance_score": 95,
        "reasons": ["Menyebut iPhone 13 Pro Max", "Ada harga"],
        "snippet": "Dijual iPhone 13 Pro Max 256GB Rp 12.500.000",
    }


SOURCE = "Dijual iPhone 13 Pro Max 256GB Rp 12.500.000 COD Tangerang"


class TestValidPayload:
    def test_valid(self):
        result = validate_llm_payload(valid_payload(), SOURCE)
        assert result.ok
        assert result.data["category"] == "menjual"
        assert result.data["engine"] == "ollama"
        assert result.data["price_amount"] == 12_500_000
        assert result.errors == []


class TestInvalidPayload:
    def test_non_dict(self):
        result = validate_llm_payload(["bukan", "dict"], SOURCE)
        assert not result.ok
        assert result.data is None

    def test_bad_category_falls_back(self):
        payload = valid_payload() | {"category": "promosi"}
        result = validate_llm_payload(payload, SOURCE)
        assert result.ok
        assert result.data["category"] == "tidak relevan"
        assert any("Kategori" in e for e in result.errors)

    def test_score_clamped(self):
        result = validate_llm_payload(valid_payload() | {"relevance_score": 500}, SOURCE)
        assert result.data["relevance_score"] == 100
        result = validate_llm_payload(valid_payload() | {"relevance_score": -20}, SOURCE)
        assert result.data["relevance_score"] == 0

    def test_non_numeric_score_defaults_zero(self):
        result = validate_llm_payload(valid_payload() | {"relevance_score": "tinggi"}, SOURCE)
        assert result.data["relevance_score"] == 0

    def test_price_string_parsed(self):
        result = validate_llm_payload(valid_payload() | {"price_amount": "12500000"}, SOURCE)
        assert result.data["price_amount"] == 12_500_000

    def test_absurd_price_dropped(self):
        result = validate_llm_payload(valid_payload() | {"price_amount": "999999999999"}, SOURCE)
        assert result.data["price_amount"] is None

    def test_details_non_dict_dropped(self):
        result = validate_llm_payload(valid_payload() | {"details": "256 GB"}, SOURCE)
        assert result.data["details"] == {}

    def test_reasons_string_becomes_list(self):
        result = validate_llm_payload(valid_payload() | {"reasons": "hanya satu"}, SOURCE)
        assert result.data["reasons"] == ["hanya satu"]

    def test_model_without_iphone_prefix_normalized(self):
        result = validate_llm_payload(valid_payload() | {"iphone_model": "13 Pro Max"}, SOURCE)
        assert result.data["iphone_model"] == "iPhone 13 Pro Max"

    def test_price_kind_invalid_reset(self):
        result = validate_llm_payload(valid_payload() | {"price_kind": "murah"}, SOURCE)
        assert result.data["price_kind"] == "tidak diketahui"


class TestAntiHallucination:
    def test_iphone_category_forced_irrelevant_without_iphone_in_source(self):
        text = "Jual kulkas 2 pintu, harga 2 juta"
        payload = valid_payload() | {"category": "menjual", "relevance_score": 90}
        result = validate_llm_payload(payload, text)
        assert result.data["category"] == "tidak relevan"
        assert result.data["relevance_score"] <= 20
        assert any("tidak menyebut iPhone" in e for e in result.errors)

    def test_snippet_falls_back_to_source(self):
        payload = valid_payload()
        payload.pop("snippet")
        result = validate_llm_payload(payload, SOURCE)
        assert result.data["snippet"]
        assert "iPhone" in result.data["snippet"]


class TestPromptBuilding:
    def test_prompt_contains_data_delimiters_and_rules(self):
        prompt = build_prompt("Dijual iPhone 13", max_chars=100)
        assert "<<<POST>>>" in prompt
        assert "<<<END>>>" in prompt
        assert "untrusted DATA" in prompt
        assert "JSON" in prompt

    def test_prompt_truncates_long_input(self):
        prompt = build_prompt("x" * 5000, max_chars=100)
        body = prompt.split("<<<POST>>>")[1].split("<<<END>>>")[0]
        assert len(body.strip()) <= 101

    def test_local_endpoint_check(self):
        assert is_local_endpoint("http://127.0.0.1:11434")
        assert is_local_endpoint("http://localhost:11434")
        assert not is_local_endpoint("https://api.openai.com")

    def test_summary_flags_remote_endpoint(self):
        config = OllamaConfig(base_url="https://example.com", model="x")
        assert summarize_config(config)["lokal"] == "TIDAK"


class TestOfflineBehaviour:
    def test_connection_check_to_closed_port_does_not_raise(self):
        from fbworkspace.ollama_client import check_connection

        status = check_connection(
            OllamaConfig(base_url="http://127.0.0.1:9", model="tidak-ada", timeout=1.0)
        )
        assert status.ok is False
        assert status.message

    def test_remote_endpoint_blocked(self):
        from fbworkspace.ollama_client import check_connection

        status = check_connection(OllamaConfig(base_url="https://example.com", model="x"))
        assert status.ok is False
        assert "localhost" in status.message
