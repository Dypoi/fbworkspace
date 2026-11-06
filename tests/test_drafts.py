"""Uji draf respons: netral, tanpa klaim karangan, tidak pernah terkirim."""

from __future__ import annotations

from fbworkspace.drafts import (
    BusinessProfile,
    build_draft,
    contains_forbidden_claim,
)


class TestDraftGeneration:
    def test_buyer_post_gets_seller_style_draft(self):
        analysis = {"category": "mencari", "iphone_model": "iPhone 11"}
        draft = build_draft({}, analysis)
        assert draft is not None
        assert "iPhone 11" in draft.body
        assert "[NAMA TOKO]" in draft.body
        assert "belum bisa saya pastikan stok" in draft.body.lower()

    def test_seller_post_draft_asks_for_details(self):
        draft = build_draft({}, {"category": "menjual", "iphone_model": "iPhone 13"})
        assert "kondisi" in draft.body.lower()
        assert draft.title

    def test_discussion_draft(self):
        draft = build_draft({}, {"category": "diskusi", "iphone_model": "iPhone 15 Pro"})
        assert draft is not None
        assert "iPhone 15 Pro" in draft.body

    def test_irrelevant_has_no_draft(self):
        assert build_draft({}, {"category": "tidak relevan"}) is None

    def test_unknown_model_falls_back_generic(self):
        draft = build_draft({}, {"category": "mencari", "iphone_model": "tidak diketahui"})
        assert "iPhone" in draft.body
        assert "tidak diketahui" not in draft.body


class TestNoInventedClaims:
    def test_no_forbidden_claims_in_any_template(self):
        for category in ("mencari", "menjual", "diskusi"):
            draft = build_draft({}, {"category": category, "iphone_model": "iPhone 13"})
            assert contains_forbidden_claim(draft.body) == []

    def test_no_specific_prices_or_warranty_years(self):
        import re

        for category in ("mencari", "menjual", "diskusi"):
            body = build_draft({}, {"category": category, "iphone_model": "iPhone 13"}).body
            assert not re.search(r"Rp\s?\d", body)
            assert not re.search(r"garansi\s+\d+\s+tahun", body, re.I)

    def test_notes_remind_manual_sending(self):
        draft = build_draft({}, {"category": "mencari"})
        assert any("TIDAK dikirim otomatis" in n for n in draft.notes)


class TestProfile:
    def test_filled_profile_replaces_placeholders(self):
        profile = BusinessProfile("Toko Uji", "Tangerang", "09.00-21.00")
        draft = build_draft({}, {"category": "menjual"}, profile)
        assert "Toko Uji" in draft.body
        assert "[NAMA TOKO]" not in draft.body

    def test_incomplete_profile_warns(self):
        draft = build_draft({}, {"category": "menjual"}, BusinessProfile())
        assert any("belum lengkap" in n for n in draft.notes)

    def test_is_filled(self):
        assert BusinessProfile("A", "B", "C").is_filled()
        assert not BusinessProfile().is_filled()
