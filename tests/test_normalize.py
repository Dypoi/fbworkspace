"""Uji normalisasi teks, tautan, harga, dan waktu."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fbworkspace.normalize import (
    canonical_post_url,
    collapse_ws,
    derive_dedupe_key,
    extract_post_ids_from_url,
    find_prices,
    normalize_text,
    parse_group_urls,
    parse_posted_at,
    strip_invisible_noise,
    text_hash,
    validate_group_url,
)


class TestTextNormalization:
    def test_collapses_whitespace_and_keeps_paragraphs(self):
        raw = "Halo   dunia\r\n\r\n\r\nbaris\tkedua  "
        assert normalize_text(raw) == "Halo dunia\n\nbaris kedua"

    def test_removes_zero_width_and_normalizes_quotes(self):
        raw = "iPhone\u200b 13 \u201cPro\u201d \u2014 mulus"
        out = normalize_text(raw)
        assert "\u200b" not in out
        assert '"Pro"' in out
        assert " - mulus" in out

    def test_hash_is_stable_across_spacing_and_case(self):
        assert text_hash("iPhone  13 PRO") == text_hash("iphone 13 pro")

    def test_hash_differs_for_different_text(self):
        assert text_hash("iphone 13") != text_hash("iphone 14")

    def test_empty_input(self):
        assert normalize_text(None) == ""
        assert collapse_ws("   ") == ""


class TestGroupUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.facebook.com/groups/123456789/",
            "http://facebook.com/groups/123456789",
            "https://m.facebook.com/groups/123456789/",
            "https://web.facebook.com/groups/123456789/posts/999/",
            "facebook.com/groups/123456789",
        ],
    )
    def test_accepts_group_links(self, url):
        result = validate_group_url(url)
        assert result.ok, result.message
        assert result.normalized.startswith("https://www.facebook.com/groups/")

    def test_normalizes_post_link_to_group_root(self):
        result = validate_group_url("https://www.facebook.com/groups/123/posts/999")
        assert result.ok
        assert result.normalized == "https://www.facebook.com/groups/123/"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "https://example.com/groups/123",
            "https://www.facebook.com/marketplace/item/123",
            "https://www.facebook.com/johndoe",
            "javascript:alert(1)",
            "https://evil-facebook.com/groups/123",
        ],
    )
    def test_rejects_non_group_links(self, url):
        assert not validate_group_url(url).ok

    def test_share_link_is_accepted_with_warning(self):
        result = validate_group_url("https://www.facebook.com/share/g/abc123/")
        assert result.ok
        assert result.warning

    def test_parse_multiple_urls_deduplicates(self):
        block = (
            "https://www.facebook.com/groups/1/\n"
            "https://www.facebook.com/groups/2/\n"
            "https://www.facebook.com/groups/1/\n"
        )
        results = parse_group_urls(block)
        assert len(results) == 2
        assert all(r.ok for r in results)


class TestPostIdsAndDedupe:
    def test_extracts_post_id(self):
        assert extract_post_ids_from_url(
            "https://www.facebook.com/groups/1/posts/987654321/"
        ) == ["987654321"]

    def test_extracts_story_fbid(self):
        ids = extract_post_ids_from_url(
            "https://www.facebook.com/permalink.php?story_fbid=111&id=222"
        )
        assert "111" in ids

    def test_dedupe_prefers_url_id(self):
        key, method = derive_dedupe_key("https://www.facebook.com/groups/1/posts/55", "apa saja")
        assert key == "urlid:55"
        assert method == "url_id"

    def test_dedupe_falls_back_to_text_hash(self):
        key, method = derive_dedupe_key(None, "Halo  Dunia")
        assert method == "text_hash"
        assert key == f"texthash:{text_hash('Halo  Dunia')}"

    def test_canonical_url_strips_tracking(self):
        url = "https://m.facebook.com/groups/1/posts/55/?__cft__=abc&ref=1"
        assert canonical_post_url(url) == "https://www.facebook.com/groups/1/posts/55"

    def test_canonical_url_rejects_other_domains(self):
        assert canonical_post_url("https://example.com/groups/1") == ""


class TestPriceParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Dijual Rp 12.500.000 nego", 12_500_000),
            ("harga 12,5jt", 12_500_000),
            ("budget 4jt", 4_000_000),
            ("850rb aja", 850_000),
            ("harga 8 juta", 8_000_000),
            ("Rp 2.900.000", 2_900_000),
            ("12.500.000", 12_500_000),
        ],
    )
    def test_detects_indonesian_prices(self, text, expected):
        matches = find_prices(text)
        assert matches, f"tidak ada harga terdeteksi pada: {text}"
        assert matches[0].amount == expected

    def test_does_not_treat_model_number_as_price(self):
        assert find_prices("iPhone 11 Pro Max 64GB mulus") == []

    def test_ignores_absurd_amounts(self):
        assert find_prices("harga 5 rupiah") == []

    def test_does_not_confuse_battery_health(self):
        assert find_prices("BH 89% mulus, garansi aman") == []


class TestPostedAt:
    def test_absolute_date(self):
        iso, precision, _note = parse_posted_at("5 November 2025 14:30")
        assert iso.startswith("2025-11-05T14:30")
        assert precision == "tepat"

    def test_date_without_time_is_marked_approx(self):
        _iso, precision, _note = parse_posted_at("5 November 2025")
        assert precision == "perkiraan"

    def test_relative_time_is_approx(self):
        ref = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        iso, precision, note = parse_posted_at("3 jam lalu", reference=ref)
        assert iso.startswith("2026-01-10T09:00")
        assert precision == "perkiraan"
        assert "relatif" in note.lower()

    def test_unknown_format_returns_none(self):
        iso, precision, _note = parse_posted_at("kemarin sore")
        assert iso is None
        assert precision == "tidak diketahui"

    def test_none_input(self):
        iso, precision, _note = parse_posted_at(None)
        assert iso is None
        assert precision == "tidak diketahui"


class TestStripInvisibleNoise:
    def test_removes_invisible_format_characters(self):
        text = "Dijual iPhone\u034f X\u200b 64GB"
        cleaned = strip_invisible_noise(text)
        assert "\u034f" not in cleaned
        assert "\u200b" not in cleaned
        assert "iPhone" in cleaned and "64GB" in cleaned

    def test_drops_facebook_label_lines(self):
        text = "Facebook | Facebook | Facebook\nDijual iPhone X 64GB"
        cleaned = strip_invisible_noise(text)
        assert "Dijual iPhone X 64GB" in cleaned
        assert "Facebook" not in cleaned

    def test_drops_letter_spam_lines(self):
        spam = "s\u034f t\u034f o\u034f d\u034f p\u034f e\u034f r\u034f S\u034f n\u034f o"
        text = f"{spam}\nDijual iPhone X 64GB mulus 5jt"
        cleaned = strip_invisible_noise(text)
        assert "Dijual iPhone X 64GB mulus 5jt" in cleaned
        assert "p e r" not in cleaned

    def test_keeps_normal_post_text(self):
        text = "ip11/64 ibox | bh 74 kel hp+box | 4 jt ambil | lok tangsel"
        assert strip_invisible_noise(text) == text
