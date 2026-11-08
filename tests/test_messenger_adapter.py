"""Uji adapter Messenger dengan halaman tiruan (tanpa browser nyata).

Yang diuji adalah *logika seleksi & pelaporan*: selector berlapis, deteksi
login/checkpoint, diagnostik, dan arah pesan. Bukan halaman Facebook asli.
"""

from __future__ import annotations

from pathlib import Path

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
                 elements=None, text="", body_text="", payload=None):
        self.url = url
        self._title = title
        self.elements = elements or {}
        self._text = text
        self._body_text = body_text or text
        self.payload = payload or {"nodes": [], "composer": None, "vw": 1280}
        self.waits = 0

    def evaluate(self, _script):
        return self.payload

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
    """Elemen palsu dengan atribut, teks, dan kotak di layar."""

    def __init__(self, text="", href="", aria_label="", children=None, box=None):
        self._text = text
        self._href = href
        self._aria = aria_label
        self._children = children or {}
        self._box = box

    def bounding_box(self):
        return self._box

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

    exe = Path("/tmp/fake-chromium")
    exe.write_text("x", encoding="utf-8") if not exe.exists() else None

    class FakePlaywright:
        chromium = type(
            "C",
            (),
            {
                "executable_path": str(exe),
                "launch_persistent_context": staticmethod(lambda *a, **k: FakeContext()),
            },
        )

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
    """Pembacaan pesan kini berbasis blok teks (`dir=auto`) + posisi layar."""

    COMPOSER = {"x": 400, "y": 700, "w": 500, "h": 40}

    def _payload(self, nodes, composer=None, vw=1280):
        return {
            "nodes": nodes,
            "composer": self.COMPOSER if composer is None else composer,
            "vw": vw,
        }

    def _node(self, text, x=380, y=400, w=300, h=40):
        return {"text": text, "x": x, "y": y, "w": w, "h": h}

    def test_messages_parsed_in_order(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [
                    self._node("harga iphone 11?", x=380, y=300),
                    self._node("2.600.000 kak", x=700, y=400),
                ]
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert read.status == "ok"
        assert [m.text for m in read.messages] == ["harga iphone 11?", "2.600.000 kak"]
        assert [m.direction for m in read.messages] == ["in", "out"]

    def test_no_candidate_messages(self, monkeypatch):
        page = FakePage(payload=self._payload([self._node("Chat Info", x=980, y=300)]))
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert read.status == "tidak_ada_pesan"
        assert "kotak tulis ada" in read.note

    def test_detail_panel_ignored(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [
                    self._node("Iphone 11 ready?", x=380, y=300),
                    self._node("Customise chat", x=980, y=300),
                    self._node("Media and files", x=980, y=360),
                ]
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert [m.text for m in read.messages] == ["Iphone 11 ready?"]

    def test_sidebar_ignored(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [
                    self._node("Pak Bagus Sunaryo", x=20, y=200, w=250),
                    self._node("Iphone 11 ready?", x=380, y=500),
                ]
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert [m.text for m in read.messages] == ["Iphone 11 ready?"]

    def test_below_composer_ignored(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [
                    self._node("pesan asli", x=380, y=500),
                    self._node("Ketik pesan", x=420, y=760),
                ]
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert [m.text for m in read.messages] == ["pesan asli"]

    def test_container_blocks_ignored(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [
                    self._node("Iphone 11 ready?", x=380, y=300),
                    self._node("seluruh halaman " * 40, x=0, y=0, w=1280, h=700),
                ]
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert len(read.messages) == 1

    def test_ui_labels_dropped(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [
                    self._node("Chat Info", x=500, y=100),
                    self._node("Iphone 11 ready?", x=380, y=300),
                ]
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert [m.text for m in read.messages] == ["Iphone 11 ready?"]
        assert any("label UI" in d for d in adapter.last_debug["dropped"])

    def test_without_composer_uses_text_column(self, monkeypatch):
        page = FakePage(
            payload=self._payload(
                [self._node("Iphone 11 ready?", x=380, y=300)], composer=False
            )
        )
        adapter = adapter_with_page(page, monkeypatch)
        read = adapter.read_thread("https://www.facebook.com/messages/t/999/")
        assert read.status == "ok"

    def test_login_on_thread(self, monkeypatch):
        page = FakePage(elements={'input[name="email"]': FakeSkill()})
        adapter = adapter_with_page(page, monkeypatch)
        assert adapter.read_thread("https://www.facebook.com/messages/t/999/").status == "perlu_login"


class TestFilterTextNodes:
    def _payload(self, nodes, composer=None, vw=1280):
        return {"nodes": nodes, "composer": composer, "vw": vw}

    def test_known_out_marks_centered_message(self):
        from fbworkspace.browser.messenger import filter_text_nodes

        payload = self._payload(
            [{"text": "2.600.000 kak", "x": 620, "y": 300, "w": 120, "h": 40}],
            composer={"x": 400, "y": 700, "w": 500, "h": 40},
        )
        messages, _dropped, _meta = filter_text_nodes(
            payload, known_out=["2.600.000 kak"]
        )
        assert messages and messages[0]["direction"] == "out"

    def test_missing_composer_is_reported(self):
        from fbworkspace.browser.messenger import filter_text_nodes

        payload = self._payload([{"text": "halo", "x": 600, "y": 300, "w": 80, "h": 30}])
        _messages, dropped, meta = filter_text_nodes(payload)
        assert meta["composer"] is False
        assert any("kotak tulis tidak ditemukan" in d for d in dropped)


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




class TestContainerScoping:
    """Regresi 8 Nov 17:33: 'Find friends' (bilah atas Facebook) terbaca sebagai pesan."""

    def _payload(self, nodes, container, composer=None, found=True):
        return {
            "nodes": nodes,
            "composer": composer if composer is not None else {"x": 420, "y": 700, "w": 600, "h": 40},
            "container": container,
            "found_container": found,
            "vw": 1280,
        }

    def _node(self, text, x=380, y=400, w=250, h=36):
        return {"text": text, "x": x, "y": y, "w": w, "h": h}

    def test_node_outside_container_is_dropped(self):
        from fbworkspace.browser.messenger import filter_text_nodes

        payload = self._payload(
            [
                self._node("Find friends", x=500, y=20),          # bilah atas
                self._node("Customise chat", x=500, y=400),       # panel detail
                self._node("Iphone 11 ready?", x=380, y=400),
            ],
            container={"x": 360, "y": 120, "w": 600, "h": 560},
        )
        messages, dropped, meta = filter_text_nodes(payload)
        assert [m["text"] for m in messages] == ["Iphone 11 ready?"]
        assert meta["container"] is True
        assert any("wadah percakapan" in d for d in dropped)

    def test_without_container_column_rules_apply(self):
        # Tanpa wadah: aturan lama (kolom kotak tulis + di atas kotak tulis) dipakai.
        from fbworkspace.browser.messenger import filter_text_nodes

        payload = self._payload(
            [
                self._node("Find friends", x=500, y=20),          # di atas kolom percakapan
                self._node("Iphone 11 ready?", x=380, y=400),
            ],
            container=None,
            found=False,
        )
        messages, _dropped, meta = filter_text_nodes(payload)
        assert "Iphone 11 ready?" in [m["text"] for m in messages]
        assert meta["container"] is False
        # "Find friends" tidak bisa dibedakan tanpa wadah -> ini keterbatasan yang
        # dicatat: wadah percakapan yang membuat penyaringan tepat.
        assert meta["found_container"] is False


class TestProfileLock:
    """Regresi 8 Nov 17:40: browser mati 16 detik setelah mulai (profil dipakai lagi)."""

    def test_lock_written_and_released(self, tmp_path):
        from fbworkspace.browser.messenger import (
            acquire_profile_lock,
            release_profile_lock,
        )

        note = acquire_profile_lock(tmp_path)
        assert note == ""
        assert (tmp_path / ".fbworkspace.lock").exists()
        release_profile_lock(tmp_path)
        assert not (tmp_path / ".fbworkspace.lock").exists()

    def test_live_lock_reports_pid(self, tmp_path, monkeypatch):
        import os as _os

        from fbworkspace.browser.messenger import LOCK_NAME, acquire_profile_lock

        monkeypatch.setattr(_os, "getpid", lambda: 424242)
        (tmp_path / LOCK_NAME).write_text(str(_os.getppid()), encoding="utf-8")
        note = acquire_profile_lock(tmp_path)
        assert "masih dipakai" in note

    def test_stale_lock_is_replaced(self, tmp_path):
        from fbworkspace.browser.messenger import LOCK_NAME, acquire_profile_lock

        (tmp_path / LOCK_NAME).write_text("999999", encoding="utf-8")
        assert acquire_profile_lock(tmp_path) == ""

    def test_browser_closed_message_has_action(self):
        note = MessengerAdapter._friendly_error(
            RuntimeError("Target page, context or browser has been closed")
        )
        assert "Tutup semua jendela Chromium" in note

    def test_process_singleton_message(self):
        note = MessengerAdapter._friendly_error(RuntimeError("ProcessSingleton lock"))
        assert "ProcessSingleton" in note

    def test_missing_chromium_detected_before_launch(self, monkeypatch):
        adapter = MessengerAdapter(profile_dir="/tmp/x")

        class FakeChromium:
            executable_path = "/tidak/ada/chromium"

        class FakePW:
            chromium = FakeChromium()

        ok, why = adapter._chromium_executable(FakePW())
        assert ok is False
        assert "playwright install chromium" in why

    def test_chromium_present(self, tmp_path):
        adapter = MessengerAdapter(profile_dir="/tmp/x")
        exe = tmp_path / "chrome"
        exe.write_text("x", encoding="utf-8")

        class FakeChromium:
            executable_path = str(exe)

        class FakePW:
            chromium = FakeChromium()

        assert adapter._chromium_executable(FakePW()) == (True, "")


class TestCloseProfileBrowsers:
    """Jendela Chromium milik aplikasi harus bisa ditutup tanpa menyentuh Chrome pribadi."""

    def test_reports_none_when_idle(self, tmp_path, monkeypatch):
        from fbworkspace.browser import messenger as mod

        monkeypatch.setattr(mod, "list_profile_browsers", lambda path: [])
        pids, note = mod.close_profile_browsers(tmp_path)
        assert pids == []
        assert "Tidak ada" in note

    def test_dry_run_does_not_kill(self, tmp_path, monkeypatch):
        from fbworkspace.browser import messenger as mod

        killed = []
        monkeypatch.setattr(
            mod, "list_profile_browsers", lambda path: [{"ProcessId": 1234}, {"ProcessId": 5678}]
        )
        monkeypatch.setattr(mod.os, "kill", lambda pid, sig: killed.append(pid))
        pids, note = mod.close_profile_browsers(tmp_path, dry_run=True)
        assert pids == [1234, 5678]
        assert killed == []
        assert "1234" in note

    def test_filter_only_uses_profile_path(self, tmp_path, monkeypatch):
        from fbworkspace.browser import messenger as mod

        class FakeRun:
            def __init__(self, *a, **k):
                self.stdout = ""

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = " ".join(cmd)
            return FakeRun()

        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setattr(mod, "_powershell", lambda: "powershell")
        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        mod.list_profile_browsers(tmp_path)
        # perintah penyaring harus memuat folder profil, bukan nama proses saja
        assert str(tmp_path) in seen["cmd"]
        assert "chrome.exe" in seen["cmd"]
