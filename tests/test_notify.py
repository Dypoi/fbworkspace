"""Uji notifikasi eskalasi (format pesan WA & normalisasi nomor)."""

from __future__ import annotations

from fbworkspace.notify import (
    HEADER,
    STOP_HEADER,
    Escalation,
    MemorySender,
    WhatsAppWebSender,
    _friendly_wa_error,
    _normalize_phone,
    build_login_escalation,
)


class FakeWaPage:
    """Halaman WhatsApp Web palsu untuk menguji jalur kirim."""

    def __init__(self, *, qr=False, composer_text="", body=""):
        self._qr = object() if qr else None
        self._composer_text = composer_text
        self._body = body
        self.url = "https://web.whatsapp.com/send?phone=6281513839973"
        self.typed: list[str] = []
        self.keys: list[str] = []

        class Keyboard:
            def __init__(self, page):
                self.page = page

            def type(self, text, delay=0):
                self.page.typed.append(text)

            def press(self, key):
                self.page.keys.append(key)

        self.keyboard = Keyboard(self)

    def title(self):
        return "WhatsApp"

    def inner_text(self, selector):
        if selector == "body":
            return self._body
        return self._composer_text

    def query_selector(self, selector):
        return self._qr

    def wait_for_selector(self, selector, timeout=None):
        if "contenteditable" in selector:
            return self
        raise TimeoutError(selector)

    def wait_for_load_state(self, *_a, **_k):
        return None

    def wait_for_timeout(self, _ms):
        return None

    def click(self):
        return None

    def goto(self, url, **_k):
        self.url = url


def _sender_with(page, monkeypatch):
    sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
    monkeypatch.setattr(sender, "_diagnose", lambda p: "url=http://x | judul='WhatsApp'")
    return sender, page


class TestSendFlow:
    def test_qr_page_detected_as_not_logged_in(self):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        page = FakeWaPage(qr=True)
        assert page.query_selector('canvas[aria-label*="Scan"]') is not None
        assert "url=" in sender._diagnose(page)

    def test_success_when_text_visible(self, monkeypatch):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        monkeypatch.setattr(
            sender,
            "_send_blocking",
            lambda pw, text, attempt=1, _p=FakeWaPage(body="cek facebook halo"): (
                True,
                "terkirim (terlihat di halaman)",
            ),
        )
        ok, note = sender.send("cek facebook halo")
        assert ok is True

    def test_diagnose_contains_state(self):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        detail = sender._diagnose(FakeWaPage(body="Scan QR code"))
        assert "url=" in detail
        assert "judul=" in detail
        assert "teks=" in detail

    def test_non_blocking_returns_quickly(self, monkeypatch):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        started = []

        monkeypatch.setattr(
            sender,
            "_send_blocking",
            lambda pw, text, attempt=1: started.append(text) or (True, "terkirim"),
        )
        ok, note = sender.send("halo", blocking=False)
        assert ok is True
        assert "latar belakang" in note


class TestFriendlyErrors:
    def test_missing_chromium(self):
        note = _friendly_wa_error(RuntimeError("Executable doesn't exist at /x"))
        assert "playwright install chromium" in note

    def test_profile_in_use(self):
        note = _friendly_wa_error(RuntimeError("ProcessSingleton: profile in use"))
        assert "dipakai jendela lain" in note

    def test_timeout(self):
        note = _friendly_wa_error(TimeoutError("Timeout 60000ms exceeded"))
        assert "batas waktu" in note


class TestEscalationText:
    def test_header_comes_first(self):
        text = Escalation(
            chat_name="Rian S.",
            message="bisa kurang ga kak?",
            reason="nego",
            url="https://www.facebook.com/messages/t/123",
            when="06/11 21:04",
        ).text()
        lines = text.splitlines()
        assert lines[0] == HEADER
        assert HEADER == "cek facebook ada chat yang bukan ranah saya"
        assert "Rian S." in text
        assert "bisa kurang ga kak?" in text
        assert "nego" in text
        assert "https://www.facebook.com/messages/t/123" in text

    def test_minimal_escalation(self):
        text = Escalation(chat_name="", message="", reason="di luar template").text()
        assert text.startswith(HEADER)
        assert "di luar template" in text

    def test_long_message_truncated(self):
        text = Escalation(chat_name="X", message="a" * 500, reason="r").text()
        assert len(text) <= 900
        assert "…" in text

    def test_login_escalation(self):
        escalation = build_login_escalation()
        assert escalation.header == STOP_HEADER
        assert "login" in escalation.text()


class TestPhone:
    def test_local_number_becomes_62(self):
        assert _normalize_phone("081513839973") == "6281513839973"

    def test_already_international(self):
        assert _normalize_phone("6281513839973") == "6281513839973"

    def test_without_leading_zero(self):
        assert _normalize_phone("81513839973") == "6281513839973"

    def test_with_symbols(self):
        assert _normalize_phone("+62 815-1383-9973") == "6281513839973"

    def test_empty(self):
        assert _normalize_phone("") == ""


class TestSender:
    def test_url_contains_target_and_text(self):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        url = sender.url_for("cek facebook")
        assert "web.whatsapp.com/send" in url
        assert "phone=6281513839973" in url
        assert "cek%20facebook" in url

    def test_memory_sender_records(self):
        sender = MemorySender()
        ok, _ = sender.send("halo")
        assert ok is True
        assert sender.sent == ["halo"]

    def test_memory_sender_failure(self):
        sender = MemorySender(ok=False, message="QR belum discan")
        ok, message = sender.send("halo")
        assert ok is False
        assert sender.sent == []
        assert "QR" in message


class TestWaComposerSelectors:
    """WA gagal karena kotak tulis tidak ditemukan; pastikan pencariannya berlapis."""

    def test_footer_composer_tried_first(self):
        from fbworkspace.notify import _WA_COMPOSER

        first = _WA_COMPOSER.split(",")[0].strip()
        assert "footer" in first and "contenteditable" in first

    def test_has_generic_fallback(self):
        from fbworkspace.notify import _WA_COMPOSER

        selectors = [s.strip() for s in _WA_COMPOSER.split(",") if s.strip()]
        assert any("spellcheck" in s or 'role="textbox"' in s for s in selectors)
        assert len(selectors) >= 5

    def test_find_composer_scans_all_selectors(self):
        from fbworkspace.notify import _find_wa_composer

        class Page:
            def __init__(self):
                self.tried = []

            def wait_for_selector(self, selector, timeout=0):
                self.tried.append(selector)
                return object() if len(self.tried) == 4 else None

            def query_selector_all(self, selector):
                return []

        page = Page()
        found = _find_wa_composer(page, timeout_ms=1)
        assert found is not None
        assert len(page.tried) == 4

    def test_browser_block_detected(self):
        from fbworkspace.notify import WhatsAppWebSender

        class Page:
            def inner_text(self, selector):
                return (
                    "WhatsApp dapat digunakan di Google Chrome 100+ Untuk menggunakan "
                    "WhatsApp, perbarui Chrome atau gunakan Mozilla Firefox"
                )

        assert WhatsAppWebSender._browser_blocked(Page()) is True

    def test_browser_block_not_flagged_on_normal_page(self):
        from fbworkspace.notify import WhatsAppWebSender

        class Page:
            def inner_text(self, selector):
                return "Chats Status Channels Komunitas"

        assert WhatsAppWebSender._browser_blocked(Page()) is False


class TestSendReliable:
    """Bug nyata 8 Nov: `notify --test --show` berhasil, worker (headless) gagal.

    Worker kini mencoba beberapa kali dan berpindah ke jendela terlihat.
    """

    def test_retries_and_reports_attempt(self, monkeypatch, tmp_path):
        sender = WhatsAppWebSender(profile_dir=tmp_path / "wa", phone="081513839973")
        assert sender.headless_works() is True  # belum ada catatan: mulai headless
        calls: list[tuple[bool, int]] = []

        def fake_send(self, text, *, blocking=True, attempt=1):
            calls.append((self.headless, attempt))
            return (attempt >= 2), ("terkirim" if attempt >= 2 else "kotak tulis tidak ditemukan")

        monkeypatch.setattr(WhatsAppWebSender, "send", fake_send)

        ok, note = sender.send_reliable("halo", tries=3)
        assert ok is True
        assert "percobaan 2" in note
        # percobaan 1 headless, percobaan 2 memakai jendela terlihat
        assert calls[0] == (True, 1)
        assert calls[1] == (False, 2)
        # hasilnya diingat: percobaan berikutnya langsung jendela terlihat
        assert sender.headless_works() is False

    def test_gives_up_after_all_tries(self, monkeypatch):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")

        monkeypatch.setattr(
            WhatsAppWebSender,
            "send",
            lambda self, text, *, blocking=True, attempt=1: (False, "gagal"),
        )
        ok, note = sender.send_reliable("halo", tries=3)
        assert ok is False
        assert "coba 3" in note

    def test_no_retry_when_not_logged_in(self, monkeypatch):
        sender = WhatsAppWebSender(profile_dir="/tmp/wa", phone="081513839973")
        attempts: list[int] = []

        def fake_send(self, text, *, blocking=True, attempt=1):
            attempts.append(attempt)
            return False, "WhatsApp Web belum login (perlu scan QR lewat scripts/wa_login.py)"

        monkeypatch.setattr(WhatsAppWebSender, "send", fake_send)

        ok, note = sender.send_reliable("halo", tries=3)
        assert ok is False
        assert len(attempts) == 1
        assert "belum login" in note


class TestCompareMode:
    """--compare harus menyimpulkan penyebab dengan jujur (headless vs terlihat)."""

    def test_conclusion_when_headless_fails(self, monkeypatch, capsys):
        from fbworkspace import notify as mod

        def fake_inspect(self, *, headless=True):
            return (not headless), ("ok" if not headless else "kotak tulis tidak ada")

        monkeypatch.setattr(mod.WhatsAppWebSender, "inspect", fake_inspect)
        fake_settings = type("S", (), {
            "wa_profile_dir": "/tmp/wa", "wa_phone": "081513839973",
        })()
        monkeypatch.setattr("fbworkspace.config.get_settings", lambda: fake_settings)

        code = mod.main(["--compare"])
        out = capsys.readouterr().out
        assert code == 0
        assert "FBW_WA_HEADLESS=false" in out

    def test_conclusion_when_not_ready_at_all(self, monkeypatch, capsys):
        from fbworkspace import notify as mod

        monkeypatch.setattr(
            mod.WhatsAppWebSender,
            "inspect",
            lambda self, *, headless=True: (False, "qr=True"),
        )
        fake_settings = type("S", (), {
            "wa_profile_dir": "/tmp/wa", "wa_phone": "081513839973",
        })()
        monkeypatch.setattr("fbworkspace.config.get_settings", lambda: fake_settings)

        code = mod.main(["--compare"])
        out = capsys.readouterr().out
        assert code == 1
        assert "wa_login.py" in out


class TestHeadlessMemory:
    """WhatsApp Web menolak mode headless di laptop pengguna (--compare membuktikan).

    Worker harus mengingat itu dan langsung memakai jendela terlihat.
    """

    def _sender(self, tmp_path):
        return WhatsAppWebSender(profile_dir=tmp_path / "wa_profile", phone="081513839973")

    def test_default_assumes_headless_ok(self, tmp_path):
        assert self._sender(tmp_path).headless_works() is True

    def test_records_headless_failure(self, tmp_path, monkeypatch):
        sender = self._sender(tmp_path)

        def fake_send(self, text, *, blocking=True, attempt=1):
            if self.headless:
                return False, "kotak tulis WhatsApp tidak ditemukan (ditolak_versi_chrome=True)"
            return True, "terkirim (kotak tulis kosong)"

        monkeypatch.setattr(WhatsAppWebSender, "send", fake_send)

        ok, note = sender.send_reliable("halo", tries=3)
        assert ok is True
        assert "percobaan 2" in note
        assert sender.headless_works() is False
        assert (tmp_path / "wa_mode.json").exists()

    def test_next_send_skips_headless(self, tmp_path, monkeypatch):
        sender = self._sender(tmp_path)
        sender._save_probe(headless_ok=False, reason="ditolak_versi_chrome")

        used: list[bool] = []

        def fake_send(self, text, *, blocking=True, attempt=1):
            used.append(self.headless)
            return True, "terkirim"

        monkeypatch.setattr(WhatsAppWebSender, "send", fake_send)
        sender.send_reliable("halo")
        assert used == [False], "percobaan pertama langsung memakai jendela terlihat"

    def test_corrupt_probe_file_is_ignored(self, tmp_path):
        sender = self._sender(tmp_path)
        path = tmp_path / "wa_mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bukan json", encoding="utf-8")
        assert sender.headless_works() is True


class TestRemedyFor:
    def test_closed_page_suggests_retry_not_qr(self):
        from fbworkspace.notify import remedy_for

        text = remedy_for("TargetClosedError: Target page has been closed")
        assert "tertutup" in text
        assert "scan QR" not in text

    def test_headless_rejection_names_headless(self):
        from fbworkspace.notify import remedy_for

        text = remedy_for("WhatsApp Web menolak versi Chrome bawaan Playwright")
        assert "headless" in text
        assert "FBW_WA_HEADLESS=false" in text

    def test_login_problem_suggests_qr(self):
        from fbworkspace.notify import remedy_for

        assert "scan QR" in remedy_for("perlu scan QR")
