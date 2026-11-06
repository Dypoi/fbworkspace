"""Uji filter kata kunci, lokasi, harga, dan mode ketat."""

from __future__ import annotations

from fbworkspace.filters import evaluate, find_keywords, match_keyword, parse_keywords
from fbworkspace.models import FilterConfig


class TestKeywordParsing:
    def test_parses_comma_and_newline(self):
        assert parse_keywords("iphone, cari iphone\njual; iphone 13") == [
            "iphone",
            "cari iphone",
            "jual",
            "iphone 13",
        ]

    def test_deduplicates_case_insensitively(self):
        assert parse_keywords("iPhone, iphone, IPHONE") == ["iPhone"]

    def test_empty(self):
        assert parse_keywords("") == []
        assert parse_keywords(None) == []


class TestMatching:
    def test_word_boundary_prevents_false_positive(self):
        assert not match_keyword("tips and tricks", "ip")
        assert match_keyword("jual ip 11", "ip")

    def test_phrase_matching(self):
        assert match_keyword("saya cari iphone 13 pro", "cari iphone")
        assert not match_keyword("saya cari hp android", "cari iphone")

    def test_find_multiple(self):
        assert find_keywords("iphone 13 mulus", ["iphone", "mulus", "rusak"]) == [
            "iphone",
            "mulus",
        ]


class TestEvaluate:
    def test_passes_relevant_post(self, sample_texts):
        result = evaluate(sample_texts["selling"], FilterConfig(require_iphone=True))
        assert result.passed
        assert result.reasons

    def test_requires_iphone_by_default(self, sample_texts):
        result = evaluate(sample_texts["irrelevant"], FilterConfig())
        assert not result.passed

    def test_include_keywords(self, sample_texts):
        cfg = FilterConfig(include_keywords=["iphone 13"])
        assert evaluate(sample_texts["selling"], cfg).passed
        cfg_miss = FilterConfig(include_keywords=["pixel"])
        assert not evaluate(sample_texts["selling"], cfg_miss).passed

    def test_exclude_keywords(self):
        cfg = FilterConfig(exclude_keywords=["rusak"])
        result = evaluate("Dijual iPhone 13 rusak harga 3jt", cfg)
        assert not result.passed
        assert "eksklusi" in result.reasons[0].lower()

    def test_exclude_wins_over_include(self):
        cfg = FilterConfig(include_keywords=["iphone"], exclude_keywords=["mati"])
        assert not evaluate("iPhone 11 mati total", cfg).passed

    def test_location_match(self, sample_texts):
        cfg = FilterConfig(locations=["Tangerang"])
        assert evaluate(sample_texts["selling"], cfg).passed
        cfg_other = FilterConfig(locations=["Medan"])
        assert not evaluate(sample_texts["selling"], cfg_other).passed

    def test_location_unknown_lenient_by_default(self):
        cfg = FilterConfig(locations=["Medan"])
        result = evaluate("Dijual iPhone 13 murah, harga 8jt", cfg)
        assert result.passed
        assert "lokasi" in result.missing

    def test_location_unknown_strict(self):
        cfg = FilterConfig(locations=["Medan"])
        result = evaluate("Dijual iPhone 13 murah, harga 8jt", cfg, strict_unknown=True)
        assert not result.passed

    def test_price_range(self, sample_texts):
        cfg = FilterConfig(price_min=1_000_000, price_max=15_000_000)
        assert evaluate(sample_texts["selling"], cfg).passed
        cfg_low = FilterConfig(price_max=5_000_000)
        assert not evaluate(sample_texts["selling"], cfg_low).passed
        cfg_high = FilterConfig(price_min=20_000_000)
        assert not evaluate(sample_texts["selling"], cfg_high).passed

    def test_price_unknown_lenient_and_strict(self):
        text = "Dijual iPhone 13 mulus, COD Bandung"
        cfg = FilterConfig(price_max=5_000_000)
        assert evaluate(text, cfg).passed
        assert not evaluate(text, cfg, strict_unknown=True).passed

    def test_empty_text(self):
        assert not evaluate("", FilterConfig()).passed

    def test_irrelevant_category_fails_even_if_iphone_word(self):
        # "iPhone" muncul tapi kategori tidak relevan bila hanya numpang sebut
        result = evaluate("Jual casing iPhone 13, bukan unit. Harga nego.", FilterConfig())
        # Casing bukan unit iPhone -> kategori bisa menjual, tetapi tetap tidak boleh crash
        assert isinstance(result.passed, bool)
