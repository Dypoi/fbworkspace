"""Uji ekstraksi isi postingan (bukan komentar) dari halaman grup.

Latar belakang nyata: di antarmuka grup, `div[role="article"]` ternyata hanya
cocok untuk komentar — postingan tidak memakainya. Perbaikan: pakai tautan
`/posts/` sebagai titik masuk, naik ke kartu postingan yang memuat satu postingan
saja, lalu ambil isinya sambil membuang komentar di dalam DOM.

Logika JavaScript `_CONTAINER_JS` dan `_POST_BODY_JS` juga diuji terpisah dengan
jsdom (lihat catatan di docs/TESTING.md); unit test ini menguji jalur Python-nya.
"""

from __future__ import annotations

import pytest

from fbworkspace.browser.playwright_fb import (
    DEFAULT_ARTICLE_SELECTOR,
    FEED_CARD_SELECTOR,
    POST_LINK_SELECTOR,
    PlaywrightFacebookAdapter,
    _cut_at_action_bar,
)

GROUP_URL = "https://www.facebook.com/groups/2065324413640420/"
POST_URL = "https://www.facebook.com/groups/2065324413640420/posts/3473882176117963/"
COMMENT_URL = POST_URL + "?comment_id=3473908156115365"


class FakeHandle:
    def __init__(self, element):
        self._element = element

    def as_element(self):
        return self._element


class FakeAnchor:
    def __init__(self, href: str, container=None):
        self._href = href
        self._container = container

    def get_attribute(self, name: str):
        return self._href if name == "href" else None

    def evaluate_handle(self, _script: str):
        return FakeHandle(self._container)


class FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        body: str | None = None,
        hrefs: tuple[str, ...] = (),
        nested: bool = False,
        evaluate_raises: bool = False,
        blocks: tuple = (),
        pos: str | None = None,
    ) -> None:
        self._text = text
        self._body = body
        self._hrefs = list(hrefs)
        self._blocks = list(blocks)
        self._pos = pos
        self._nested = nested
        self._evaluate_raises = evaluate_raises

    def inner_text(self) -> str:
        return self._text

    def evaluate(self, script: str):
        if self._evaluate_raises:
            raise RuntimeError("elemen hilang")
        if self._body is not None and "cloneNode" in script:
            return self._body
        return self._nested

    def get_attribute(self, name: str):
        return self._pos if name == "aria-posinset" else None

    def query_selector_all(self, selector: str):
        if "dir=" in selector:
            return list(self._blocks)
        return [FakeAnchor(href) for href in self._hrefs]


class FakePage:
    def __init__(self, anchors=(), articles=(), cards=(), link_fails: bool = False):
        self._anchors = list(anchors)
        self._articles = list(articles)
        self._cards = list(cards)
        self._link_fails = link_fails
        self.queries: list[str] = []

    def query_selector_all(self, selector: str):
        self.queries.append(selector)
        if selector == POST_LINK_SELECTOR and self._link_fails:
            raise RuntimeError("halaman berubah")
        if "/posts/" in selector:
            return self._anchors
        if selector == FEED_CARD_SELECTOR:
            return self._cards
        return self._articles


@pytest.fixture()
def adapter(tmp_path) -> PlaywrightFacebookAdapter:
    return PlaywrightFacebookAdapter(profile_dir=tmp_path / "profile")


class TestContainersFromLinks:
    def test_skips_comment_links_and_duplicates(self, adapter):
        post1 = FakeElement("post1")
        post2 = FakeElement("post2")
        page = FakePage(
            anchors=[
                FakeAnchor("https://www.facebook.com/groups/1/posts/111/", post1),
                FakeAnchor(COMMENT_URL, FakeElement("komentar")),
                FakeAnchor("https://www.facebook.com/groups/1/posts/111/?ref=x", post1),
                FakeAnchor("https://www.facebook.com/groups/1/posts/444/", post2),
            ]
        )
        containers = adapter._containers_from_links(page)
        assert containers == [post1, post2]

    def test_broken_page_returns_empty(self, adapter):
        page = FakePage(anchors=[FakeAnchor(POST_URL, FakeElement("x"))], link_fails=True)
        assert adapter._containers_from_links(page) == []

    def test_permalink_links_supported(self, adapter):
        container = FakeElement("post")
        page = FakePage(
            anchors=[
                FakeAnchor(
                    "https://www.facebook.com/groups/1/permalink/555/", container
                )
            ]
        )
        assert adapter._containers_from_links(page) == [container]

    def test_anchor_without_post_id_skipped(self, adapter):
        page = FakePage(anchors=[FakeAnchor("/groups/1/posts/?comment_id=1", None)])
        assert adapter._containers_from_links(page) == []


class TestCandidateElements:
    def test_uses_post_links_when_available(self, adapter):
        container = FakeElement("post")
        page = FakePage(anchors=[FakeAnchor(POST_URL, container)])
        assert adapter._candidate_elements(page) == [container]
        assert adapter._last_source == "post-links"

    def test_uses_feed_cards_when_no_links(self, adapter):
        card = FakeElement("kartu")
        page = FakePage(cards=[card])
        assert adapter._candidate_elements(page) == [card]
        assert adapter._last_source == "feed-cards"

    def test_falls_back_to_article_selector(self, adapter):
        article = FakeElement("artikel")
        page = FakePage(articles=[article])
        assert adapter._candidate_elements(page) == [article]
        assert adapter._last_source == "article-selector"
        assert page.queries[0] == POST_LINK_SELECTOR
        assert FEED_CARD_SELECTOR in page.queries
        assert page.queries[-1] == DEFAULT_ARTICLE_SELECTOR

    def test_custom_selector_respected(self, tmp_path):
        custom = PlaywrightFacebookAdapter(
            profile_dir=tmp_path / "p", article_selector="div.kartu"
        )
        article = FakeElement("artikel")
        page = FakePage(anchors=[FakeAnchor(POST_URL, FakeElement("post"))], articles=[article])
        assert custom._candidate_elements(page) == [article]
        assert custom._last_source == "custom-selector"
        assert page.queries == ["div.kartu"]


class TestPostExtraction:
    def test_body_js_wins_over_full_inner_text(self, adapter):
        element = FakeElement(
            "Surya Galuh · 2h\nDijual iPhone X 64GB\nLike Comment Share\nZozo · 1m brp Reply",
            body="Surya Galuh · 2h\nDijual iPhone X 64GB mulus, 5jt COD Tangerang",
            hrefs=(POST_URL,),
        )
        post = adapter._extract_post(element, GROUP_URL, {}, set())
        assert post is not None
        assert "iPhone X" in post.text
        assert "brp" not in post.text
        assert "Reply" not in post.text

    def test_falls_back_to_inner_text_when_body_js_fails(self, adapter):
        element = FakeElement(
            "Dijual iPhone X 64GB mulus, 5jt COD Tangerang",
            evaluate_raises=True,
            hrefs=(POST_URL,),
        )
        post = adapter._extract_post(element, GROUP_URL, {}, set())
        assert post is not None
        assert "iPhone X" in post.text

    def test_action_bar_cuts_off_comments(self, adapter):
        element = FakeElement(
            "Dijual iPhone X 64GB mulus, 5jt COD Tangerang\n\n"
            "Like\nComment\nShare\n\nAli: brp?\nReply",
            hrefs=(POST_URL,),
        )
        post = adapter._extract_post(element, GROUP_URL, {}, set())
        assert post is not None
        assert post.text == "Dijual iPhone X 64GB mulus, 5jt COD Tangerang"

    def test_comment_link_not_used_as_post_url(self, adapter):
        element = FakeElement(
            "Dijual iPhone X 64GB mulus, 5jt COD Tangerang",
            hrefs=(COMMENT_URL, POST_URL),
        )
        post = adapter._extract_post(element, GROUP_URL, {}, set())
        assert post is not None
        assert post.post_url is not None
        assert "comment_id" not in post.post_url
        assert "3473882176117963" in post.post_url

    def test_comment_text_still_rejected(self, adapter):
        diagnostics: dict = {}
        element = FakeElement("Zozo Yan · 1m Mahar Reply Share", hrefs=(COMMENT_URL,))
        assert adapter._extract_post(element, GROUP_URL, diagnostics, set()) is None
        assert diagnostics["skipped_comment"] == 1


class TestCutAtActionBar:
    def test_cuts_indonesian_bar(self):
        text = "Dijual iPhone X 5jt\n\nSuka\nKomentar\nBagikan\n\nBudi: nego?"
        assert _cut_at_action_bar(text) == "Dijual iPhone X 5jt\n"

    def test_leaves_text_without_action_bar(self):
        text = "Dijual iPhone X 5jt COD Tangerang"
        assert _cut_at_action_bar(text) == text


class TestFeedCardExtraction:
    """Kasus nyata grup tanpa tautan postingan (dump pengguna)."""

    def _card(self, *blocks: str, text: str = "", hrefs: tuple[str, ...] = ()):
        return FakeElement(
            text=text,
            hrefs=hrefs,
            blocks=tuple(FakeElement(block) for block in blocks),
        )

    def test_blocks_text_used_instead_of_noisy_inner_text(self, adapter):
        adapter._last_source = "feed-cards"
        card = self._card(
            "BU ip 14 256gb wifi only",
            "minus kaya di foto, satset malem ini 4,5 ambil",
        )
        # inner_text kartu penuh label "Facebook" dan huruf berjauhan.
        card._text = "Facebook | Facebook | s͏ | t͏ | o͏ | d͏ | p͏ | e͏ | r͏"
        post = adapter._extract_post(card, GROUP_URL, {}, set())
        assert post is not None
        assert "ip 14 256gb" in post.text
        assert "Facebook" not in post.text
        assert "s͏" not in post.text

    def test_post_without_link_saved_without_url(self, adapter):
        adapter._last_source = "feed-cards"
        card = self._card("ip11/64 ibox", "bh 74 kel hp+box, 4 jt ambil, lok tangsel")
        post = adapter._extract_post(card, GROUP_URL, {}, set())
        assert post is not None
        assert post.post_url is None

    def test_empty_skeleton_card_skipped_and_counted(self, adapter):
        adapter._last_source = "feed-cards"
        diagnostics: dict = {}
        card = self._card(text="Facebook | Facebook")
        assert adapter._extract_post(card, GROUP_URL, diagnostics, set()) is None
        assert diagnostics["empty_cards"] == 1

    def test_empty_card_counted_once_per_position(self, adapter):
        adapter._last_source = "feed-cards"
        diagnostics: dict = {}
        card = FakeElement(text="Facebook | Facebook", pos="4")
        for _ in range(3):
            adapter._extract_post(card, GROUP_URL, diagnostics, set())
        assert diagnostics["empty_cards"] == 1

    def test_comment_block_inside_card_ignored(self, adapter):
        adapter._last_source = "feed-cards"
        card = self._card(
            "Dijual iPhone X 64GB mulus 5jt",
            "Ali Romlan · 1h Ram brp Like Reply Share",
        )
        post = adapter._extract_post(card, GROUP_URL, {}, set())
        assert post is not None
        assert "brp" not in post.text

    def test_card_with_only_comment_text_rejected(self, adapter):
        adapter._last_source = "feed-cards"
        diagnostics: dict = {}
        card = self._card(
            "Zozo Yan · 1m Mahar Reply Share",
            text="Zozo Yan · 1m Mahar Reply Share",
        )
        assert adapter._extract_post(card, GROUP_URL, diagnostics, set()) is None
        assert diagnostics["skipped_comment"] == 1

    def test_invisible_characters_removed(self, adapter):
        adapter._last_source = "feed-cards"
        card = self._card("Dijual iPhone X 64GB 5jt\u034f Tangerang")
        post = adapter._extract_post(card, GROUP_URL, {}, set())
        assert post is not None
        assert "\u034f" not in post.text
        assert "Tangerang" in post.text
