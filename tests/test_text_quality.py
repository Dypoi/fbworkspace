"""Uji penyaring komentar/reply.

Latar belakang nyata: satu job membaca 31 "postingan" yang ternyata komentar
Facebook (`Ali Romlan · 1hRam brpLikeReplyShare`, dst). Semua lolos ke database
sebagai postingan, lalu 31/31 digugurkan filter "wajib iPhone" — hasilnya nol
padahal halaman grup berisi postingan. Tes ini mengunci perilaku yang benar:

1. `looks_like_comment()` menandai pola komentar dan tidak memakan postingan sah.
2. Adapter tidak menyimpan komentar, menghitungnya, dan menyimpan contoh teks.
3. Data lama yang sudah terlanjur tersimpan bisa dibersihkan.
"""

from __future__ import annotations

import pytest

from fbworkspace.browser.base import CollectOutcome
from fbworkspace.browser.playwright_fb import PlaywrightFacebookAdapter
from fbworkspace.models import AccessStatus, JobStatus
from fbworkspace.normalize import looks_like_comment
from fbworkspace.worker import _map_outcome_status

# Potongan asli dari ekspor pengguna (job #6 dan job sebelumnya).
COMMENT_SAMPLES = [
    "Ali Romlan · 1hRam brpLikeReplyShare",
    "Ayubb · 5d infinix note 60 pro 8/256 nih Reply 1",
    "Ramdan Hermansyah · 2d 38 pm mas Reply",
    "Yogga Kdanee · 5h · TT bisa ga mas Reply",
    "Raka Barajasena · 5m ip 12 pm 256 ibox, nambah brapa? Reply",
    "Dicky Riandy · 1d masih? Reply See translation",
    "Pra Tama · 50m infinix note 40 pro nya berapa? Reply Edited",
    "Nabila Syakieb · 35mtt ke xs minat gaLikeReplySee translationShare",
    "Wandi · 2d 4.450 gas gue kelokLikeReplyShare1",
    "Hanip Bejo · 57m 6,3 Reply Edited",
]

POST_SAMPLES = [
    "Dijual iPhone X 64GB mulus, harga 5jt COD Tangerang.",
    "Cari iPhone X budget 3jt area Bandung, DM ya.",
    "Dijual iPhone 13 Pro Max 256GB iBox fullset, BH 89%, COD Jakarta.",
    "Dijual kulkas 2 pintu murah, COD Depok.",
]

GROUP_URL = "https://www.facebook.com/groups/2065324413640420/"


class FakeAnchor:
    def __init__(self, href: str) -> None:
        self._href = href

    def get_attribute(self, name: str) -> str | None:
        return self._href if name == "href" else None


class FakeElement:
    """Elemen Playwright tiruan: cukup untuk `_extract_post`."""

    def __init__(
        self,
        text: str,
        hrefs: tuple[str, ...] = (),
        *,
        nested: bool = False,
    ) -> None:
        self._text = text
        self._hrefs = list(hrefs)
        self._nested = nested

    def inner_text(self) -> str:
        return self._text

    def evaluate(self, _script: str) -> bool:
        return self._nested

    def query_selector_all(self, _selector: str) -> list[FakeAnchor]:
        return [FakeAnchor(href) for href in self._hrefs]


@pytest.fixture()
def adapter(tmp_path) -> PlaywrightFacebookAdapter:
    return PlaywrightFacebookAdapter(profile_dir=tmp_path / "profile")


class TestLooksLikeComment:
    @pytest.mark.parametrize("text", COMMENT_SAMPLES)
    def test_comment_patterns_detected(self, text):
        assert looks_like_comment(text) is True

    @pytest.mark.parametrize("text", POST_SAMPLES)
    def test_real_posts_not_flagged(self, text):
        assert looks_like_comment(text) is False

    def test_empty_text(self):
        assert looks_like_comment("   ") is False

    def test_short_post_ending_with_reply_word_inside_is_fine(self):
        # Kata "reply" di tengah kalimat bukan penanda komentar.
        assert looks_like_comment("Dijual iPhone X, fast reply via DM, COD Tangerang.") is False


class TestAdapterSkipsComments:
    def test_comment_not_returned_and_counted(self, adapter):
        diagnostics: dict = {}
        skipped: set[str] = set()
        element = FakeElement(COMMENT_SAMPLES[0])
        assert adapter._extract_post(element, GROUP_URL, diagnostics, skipped) is None
        assert diagnostics["skipped_comment"] == 1
        assert diagnostics["sample_texts"]
        assert "Ali Romlan" in diagnostics["sample_texts"][0]

    def test_same_comment_counted_once(self, adapter):
        diagnostics: dict = {}
        skipped: set[str] = set()
        element = FakeElement(COMMENT_SAMPLES[1])
        for _ in range(3):
            assert adapter._extract_post(element, GROUP_URL, diagnostics, skipped) is None
        assert diagnostics["skipped_comment"] == 1

    def test_nested_article_is_skipped(self, adapter):
        diagnostics: dict = {}
        element = FakeElement(POST_SAMPLES[0], nested=True)
        assert adapter._extract_post(element, GROUP_URL, diagnostics, set()) is None
        assert diagnostics.get("skipped_comment", 0) == 0

    def test_comment_only_permalink_is_skipped(self, adapter):
        diagnostics: dict = {}
        element = FakeElement(
            "38 pm mas, nego dikit boleh kok",
            hrefs=(
                "https://www.facebook.com/groups/1/posts/2/?comment_id=33",
            ),
        )
        assert adapter._extract_post(element, GROUP_URL, diagnostics, set()) is None
        assert diagnostics["skipped_comment"] == 1

    def test_real_post_is_returned(self, adapter):
        diagnostics: dict = {}
        element = FakeElement(
            POST_SAMPLES[0],
            hrefs=("https://www.facebook.com/groups/1/posts/999/?__cft__=x",),
        )
        post = adapter._extract_post(element, GROUP_URL, diagnostics, set())
        assert post is not None
        assert "iPhone X" in post.text
        assert post.post_url and "999" in post.post_url
        assert "__cft__" not in post.post_url
        assert diagnostics.get("skipped_comment", 0) == 0

    def test_post_without_link_still_returned(self, adapter):
        post = adapter._extract_post(FakeElement(POST_SAMPLES[1]), GROUP_URL)
        assert post is not None
        assert post.post_url is None


class TestCleanupOldRows:
    def test_find_and_delete_suspicious_rows(self, db):
        comment_ids = [
            db.insert_post(text=text, source="browser", filter_passed=False)[0]
            for text in COMMENT_SAMPLES[:2]
        ]
        real_ids = [
            db.insert_post(text=text, source="browser", filter_passed=True)[0]
            for text in POST_SAMPLES[:2]
        ]
        db.save_analysis(
            comment_ids[0],
            {
                "engine": "rule",
                "category": "tidak relevan",
                "iphone_model": "tidak diketahui",
                "relevance_score": 0,
                "reasons": [],
                "details": {},
            },
        )

        suspects = db.find_suspicious_posts()
        assert {row["id"] for row in suspects} == set(comment_ids)

        removed = db.delete_suspicious_posts()
        assert removed == 2
        assert db.count_posts() == 2
        for post_id in real_ids:
            assert db.get_post(post_id) is not None
        for post_id in comment_ids:
            assert db.get_post(post_id) is None
            assert db.get_analysis(post_id) is None

    def test_cleanup_without_suspects(self, db):
        db.insert_post(text=POST_SAMPLES[0], source="manual", filter_passed=True)
        assert db.find_suspicious_posts() == []
        assert db.delete_suspicious_posts() == 0
        assert db.count_posts() == 1


class TestWorkerMessage:
    def test_skipped_comments_reported_in_job_message(self):
        outcome = CollectOutcome(
            AccessStatus.OK_EMPTY,
            [],
            "Tidak ada postingan yang layak disimpan.",
        )
        status, message = _map_outcome_status(outcome, {"skipped_comment": 31})
        assert status == JobStatus.COMPLETED.value
        assert "31" in message
        assert "komentar" in message.lower()

    def test_no_skip_wording_when_zero(self):
        outcome = CollectOutcome(AccessStatus.OK, [], "Selesai membaca 3 postingan.")
        _status, message = _map_outcome_status(outcome, {})
        assert "komentar" not in message.lower()
