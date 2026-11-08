"""Uji adapter Messenger dengan halaman tiruan (tanpa browser nyata).

Yang diuji adalah *logika seleksi & pelaporan*: selector berlapis, deteksi
login/checkpoint, diagnostik, dan arah pesan. Bukan halaman Facebook asli.
"""

from __future__ import annotations

from fbworkspace.browser.messenger import (
    MessengerAdapter,
    _direction,
    _msg_key,
    _thread_key,
    diagnose,
)


class FakePage:
    """Halaman palsu: cukup untuk query_selector / query_selector_all."""

    def __init__(self, *, url="https://www.facebook.com/messages/", title="Messenger",
                 elements=None, text="", body_text=""):
        self.url = url
        self._title = title
        self.elements = elements or {}
        self._text = text
        self._body_text = body_text or text
        self.waits = 0

    def query_selector(self, selector):
        found = self.elements.get(selector)
        if isinstance(found, list):
            return found[0] if found else None
        return found

    def query_selector_all(self, selector):
        found = self.elements.get(selector) or []
        return found if isinstance(found, list) else [found]

    def title(self):
        return self._title

    def inner_text(self, selector):
        return self._body_text

    def goto(self, url, **_):
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def wait_for_selector(self, selector, **_kwargs):
        found = self.query_selector(selector)
        if found is None:
            raise TimeoutError(f"{selector} tidak muncul")
        return found

    @property
    def mouse(self):
        return self

    def wheel(self, *_args):
        return None


class FakeSkill:
    """Elemen palsu dengan atribut & teks."""

    def __init__(self, text="", href="", aria_label="", children=None):
        self._text = text
        self._href = href
        self._aria = aria_label
        self._children = children or {}

    def get_attribute(self, name):
        if name == "href":
            return self._href
        if name == "aria-label":
            return self._aria
        return None

    def inner_text(self):
        return self._text

    def query_selector(self, selector):
        return self._children.get(selector)

    def query_selector_all(self, selector):
        found = self._children.get(selector) or []
        return found if isinstance(found, list) else [found]


def adapter_with_page(page, monkeypatch):
    """Adapter yang memakai halaman palsu, tanpa Playwright."""
    adapter = MessengerAdapter(profile_dir="/tmp/tidak-dipakai")

    class FakeContext:
        pages = [page]

        def close(self):
            return None

    class FakePlaywright:
        chromium = type("C", (), {"launch_persistent_context": staticmethod(lambda *a, **k: FakeContext())})

        def stop(self):
            return None

    monkeypatch.setattr(adapter, "_launch", lambda: FakePlaywright())
    return adapter


class TestHelpers:
    def test_thread_key_from_url(self):
        assert _thread_key("https://www.facebook.com/messages/t/123456/?x=1") == "123456"

    def test_thread_key_fallback(self):
        assert _thread_key("https://www.facebook.com/messages/") == "https://www.facebook.com/messages/"

    def test_msg_key_stable(self):
        assert _msg_key("123", 4, "halo") == _msg_key("123", 4, "halo")
        assert _msg_key("123", 4, "halo") != _msg_key("123", 5, "halo")

    def test_direction_from_aria_label(self):
        row = FakeSkill(aria_label="Anda kirim: halo")
        assert _direction(row, "halo", []) == "out"

    def test_direction_received(self):
        row = FakeSkill(aria_label="Pesan diterima")
        assert _direction(row, "halo", []) == "in"

    def test_direction_from_known_out(self):
        row = FakeSkill()
        assert _direction(row, "2.600.000 kak", ["2.600.000 kak"]) == "out"

    def test_direction_unknown_defaults_in(self):
        assert _direction(FakeSkill(), "pesan pembeli", []) == "in"


class TestListThreads:
    def test_threads_found(self, monkeypatch):
        anchors = [
            FakeSkill(text="Budi\nhalo", href="/messages/t/111/", aria_label="Budi"),
            FakeSkill(text="Sari\nhalo", href="/messages/t/222/", aria_label="Sari, belum dibaca"),
            FakeSkill(text="ganda", href="/messages/t/111/", aria_label="Budi"),
        ]
        page = FakePage(elements={'a[href*="/messages/t/"]': anchors})
        adapter = adapter_with_page(page, monkeypatch)

        result = adapter.list_threads_detailed(limit=5)

        assert result.status == "ok"
        assert [t.key for t in result.threads] == ["111", "222"]
        assert result.threads[0].url.endswith("/messages/t/111/")
        assert result.threads[1].unread is True
        assert result.diagnostics["thread_links"] == 3

    def test_login_form_detected(self, monkeypatch):
        page = FakePage(
            url="https://www.facebook.com/login",
            elements={'input[name="email"]': FakeSkill(), 'a[href*="/messages/t/"]': []},
        )
        adapter = adapter_with_page(page, monkeypatch)
        result = adapter.list_threads_detailed()
        assert result.status == "perlu_login"

    def test_checkpoint_detected(self, monkeypatch):
        page = FakePage(
            url="https://www.facebook.com/checkpoint/123",
            title="Security check",
            text="Kami perlu memastikan ini Anda",
            elements={'a[href*="/messages/t/"]': []},
        )
        adapter = adapter_with_page(page, monkeypatch)
        result = adapter.list_threads_detailed()
        assert result.status == "pemblokiran"
        assert result.diagnostics["reason"]

    def test_no_chat_reports_diagnostics(self, monkeypatch):
        page = FakePage(text="Selamat datang di Messenger", elements={'a[href*="/messages/t/"]': []})
        adapter = adapter_with_page(page, monkeypatch)
        result = adapter.list_threads_detailed()
        assert result.status == "tidak_ada_chat"
        assert result.diagnostics["thread_links"] == 0
        assert result.diagnostics["text"]
        # halaman diberi kesempatan memuat sebelum disimpulkan kosong
        assert page.waits >= 1

    def test_list_threads_shortcut(self, monkeypatch):
        page = FakePage(elements={'a[href*="/messages/t/"]': []})
        adapter = adapter_with_page(page, monkeypatch)
        threads, status = adapter.list_threads()
        assert threads == []
        assert status == "tidak_ada_chat"

    def test_error_is_status_not_exception(self, monkeypatch):
        adapter = MessengerAdapter(profile_dir="/tmp/x")

        class Boom:
            def stop(self):
                return None

        def explode():
            raise RuntimeError("Chromium tidak terpasang")

        monkeypatch.setattr(adapter, "_launch", explode)
        result = adapter.list_threads_detailed()
        assert result.status == "kesalahan"
        assert "Chromium" in result.diagnostics["error"]


class TestReadThread:
    def test_messages_parsed(self, monkeypatch):
        incoming = FakeSkill(children={'div[dir="auto"]': FakeSkill(text="harga iphone 11?")})
        outgoing = FakeSkill(
            aria_label="Anda kirim: 2.600.000 kak",
            children={'div[dir="auto"]': FakeSkill(text="2.600.000 kak")},
        )
        page = FakePage(
            elements={
                'div[role="main"] div[role="row"]': [incoming, outgoing],
                'div[dir="auto"]': FakeSkill(text="x"),
            }
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert read.status == "ok"
        assert [m.text for m in read.messages] == ["harga iphone 11?", "2.600.000 kak"]
        assert [m.direction for m in read.messages] == ["in", "out"]

    def test_no_message_rows(self, monkeypatch):
        page = FakePage(elements={'div[role="main"] div[role="row"]': []})
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert read.status == "tidak_ada_pesan"

    def test_login_on_thread(self, monkeypatch):
        page = FakePage(elements={'input[name="email"]': FakeSkill()})
        adapter = adapter_with_page(page, monkeypatch)
        assert adapter.read_thread("https://www.facebook.com/messages/t/999/").status == "perlu_login"


class TestSend:
    def test_missing_composer_is_reported(self, monkeypatch):
        page = FakePage(elements={})
        adapter = adapter_with_page(page, monkeypatch)
        ok, note = adapter.send_message("https://www.facebook.com/messages/t/1/", "halo")
        assert ok is False
        assert "kotak tulis" in note

    def test_empty_text_rejected(self):
        adapter = MessengerAdapter(profile_dir="/tmp/x")
        assert adapter.send_message("https://x/messages/t/1/", "  ") == (False, "teks kosong")


class TestUnavailable:
    def test_without_playwright(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "playwright" or name.startswith("playwright."):
                raise ImportError("tidak ada")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        adapter = MessengerAdapter(profile_dir="/tmp/x")
        ok, why = adapter.is_available()
        assert ok is False
        assert "Playwright" in why

    def test_diagnose_without_playwright(self, monkeypatch):
        monkeypatch.setattr(
            MessengerAdapter, "is_available", lambda self: (False, "Playwright tidak tersedia")
        )
        payload = diagnose("/tmp/x")
        assert payload["ok"] is False


class TestRealAccountShape:
    """Bentuk halaman nyata (laporan pengguna 8 Nov): 1 tautan /messages/t/,
    13 tautan /t/ — versi lama berhenti di selector pertama sehingga 0 chat."""

    def _page(self):
        return FakePage(
            elements={
                'a[href*="/messages/t/"]': [FakeSkill(href="/messages/t/999/")],
                'div[role="listitem"] a[href*="/t/"]': [
                    FakeSkill(href=f"/t/{100 + i}/", text=f"Chat {i}") for i in range(13)
                ],
            }
        )

    def test_all_selectors_are_combined(self, monkeypatch):
        adapter = adapter_with_page(self._page(), monkeypatch)
        result = adapter.list_threads_detailed(limit=20)
        assert result.status == "ok"
        assert len(result.threads) == 14
        assert result.threads[0].key == "999"
        assert result.diagnostics["candidates"] == 14

    def test_duplicates_removed_across_selectors(self, monkeypatch):
        page = FakePage(
            elements={
                'a[href*="/messages/t/"]': [FakeSkill(href="/messages/t/111/")],
                'div[role="listitem"] a[href*="/t/"]': [FakeSkill(href="/t/111/")],
            }
        )
        adapter = adapter_with_page(page, monkeypatch)
        result = adapter.list_threads_detailed()
        assert [t.key for t in result.threads] == ["111"]

    def test_limit_applied(self, monkeypatch):
        adapter = adapter_with_page(self._page(), monkeypatch)
        result = adapter.list_threads_detailed(limit=3)
        assert len(result.threads) == 3


class TestConversationReading:
    """Regresi nyata: worker sempat membaca label sidebar ('Chat Info') sebagai pesan."""

    def test_ui_labels_are_dropped(self):
        from fbworkspace.browser.messenger import _clean_message_text, _is_ui_noise

        assert _is_ui_noise("Chat Info") is True
        assert _is_ui_noise("Ketik pesan") is True
        assert _is_ui_noise("Active now") is True
        assert _is_ui_noise("Iphone 11 ready?") is False
        assert _clean_message_text("Chat Info\nIphone 11 ready?\nAktif sekarang") == "Iphone 11 ready?"

    def test_conversation_url_is_direct(self):
        adapter = MessengerAdapter(profile_dir="/tmp/x")
        assert adapter.conversation_url("https://www.facebook.com/t/123456/") == (
            "https://www.facebook.com/messages/t/123456/"
        )
        assert adapter.conversation_url("https://www.facebook.com/messages/t/99/?x=1") == (
            "https://www.facebook.com/messages/t/99/"
        )

    def test_sidebar_rows_not_used_as_messages(self, monkeypatch):
        # Sidebar punya 13 baris; area percakapan kosong -> harus 'tidak_ada_pesan'
        sidebar = FakeSkill(children={'div[dir="auto"]': FakeSkill(text="Chat Info")})
        page = FakePage(
            elements={
                'div[role="row"]': [sidebar] * 13,
                'div[role="main"] div[role="row"]': [],
            }
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/123/")
        assert read.status == "tidak_ada_pesan"
        assert "baris di halaman: 13" in read.note

    def test_real_message_in_conversation_area(self, monkeypatch):
        bubble = FakeSkill(children={'div[dir="auto"]': FakeSkill(text="Iphone 11 ready?")})
        header = FakeSkill(children={'div[dir="auto"]': FakeSkill(text="Chat Info")})
        page = FakePage(
            elements={
                'div[role="row"]': [header] * 13,
                'div[role="main"] div[role="row"]': [header, bubble],
            }
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/123/")
        assert read.status == "ok"
        assert [m.text for m in read.messages] == ["Iphone 11 ready?"]
