"""Uji pengisian PIN pemulihan chat — termasuk yang TIDAK boleh terjadi.

PIN hanya boleh diketik di layar "masukkan PIN pemulihan chat". Di layar
checkpoint, verifikasi identitas, kode 2FA, atau kata sandi: berhenti.
"""

from __future__ import annotations

from fbworkspace.browser.messenger import MessengerAdapter
from fbworkspace.chat_pin import (
    MAX_ATTEMPTS_PER_RUN,
    decide,
    fill_pin,
    is_valid_pin,
    looks_like_pin_prompt,
    mask,
    pin_escalation_text,
)

PIN = "102002"


class FakeInput:
    def __init__(self):
        self.typed: list[str] = []
        self.clicked = False

    def click(self):
        self.clicked = True


class FakeKeyboard:
    def __init__(self, sink: list[str]):
        self.sink = sink
        self.keys: list[str] = []

    def type(self, text, delay=0):
        self.sink.append(text)

    def press(self, key):
        self.keys.append(key)


class FakePage:
    def __init__(self, *, text="", url="https://www.facebook.com/messages/",
                 title="Messenger", pin_box=True):
        self._text = text
        self.url = url
        self._title = title
        self._pin_box = FakeInput() if pin_box else None
        self.typed: list[str] = []
        self.keyboard = FakeKeyboard(self.typed)
        self.waits = 0

    def title(self):
        return self._title

    def inner_text(self, _selector):
        return self._text

    def query_selector(self, selector):
        if self._pin_box is not None and "input" in selector:
            return self._pin_box
        return None

    def query_selector_all(self, _selector):
        return []

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def wait_for_load_state(self, *_a, **_k):
        return None


class TestValidation:
    def test_six_digits_valid(self):
        assert is_valid_pin("102002") is True

    def test_too_short_invalid(self):
        assert is_valid_pin("123") is False

    def test_letters_invalid(self):
        assert is_valid_pin("10a002") is False

    def test_empty_invalid(self):
        assert is_valid_pin("") is False

    def test_mask_hides_digits(self):
        masked = mask("102002")
        assert "102002" not in masked
        assert "6" in masked

    def test_mask_empty(self):
        assert mask("") == "(kosong)"


class TestPromptDetection:
    def test_indonesian_prompt(self):
        assert looks_like_pin_prompt("Masukkan PIN untuk memulihkan chat Anda") is True

    def test_english_prompt(self):
        assert looks_like_pin_prompt("Enter your PIN to restore your chats") is True

    def test_unrelated_text(self):
        assert looks_like_pin_prompt("Halo kak, iphone 11 ready?") is False


class TestDecision:
    def _decide(self, text, **kwargs):
        params = {"page_text": text, "pin": PIN, "auto": True}
        params.update(kwargs)
        return decide(**params)

    def test_allowed_on_pin_screen(self):
        decision = self._decide("Masukkan PIN pemulihan chat Anda")
        assert decision.allowed is True
        assert decision.keyword

    def test_empty_pin_refused(self):
        decision = self._decide("Masukkan PIN pemulihan chat Anda", pin="")
        assert decision.allowed is False
        assert "FBW_CHAT_PIN" in decision.reason

    def test_invalid_pin_refused(self):
        decision = self._decide("Masukkan PIN pemulihan chat Anda", pin="12ab")
        assert decision.allowed is False

    def test_auto_off_refused(self):
        decision = self._decide("Masukkan PIN pemulihan chat Anda", auto=False)
        assert decision.allowed is False
        assert "dimatikan" in decision.reason

    def test_checkpoint_refused(self):
        decision = self._decide("Checkpoint: konfirmasi identitas Anda")
        assert decision.allowed is False
        assert "checkpoint" in decision.reason

    def test_verification_code_refused(self):
        decision = self._decide("Masukkan kode verifikasi yang dikirim ke email")
        assert decision.allowed is False

    def test_password_refused(self):
        decision = self._decide("Masukkan kata sandi akun Anda")
        assert decision.allowed is False

    def test_two_factor_refused(self):
        decision = self._decide("Two-factor authentication diperlukan")
        assert decision.allowed is False

    def test_pin_word_in_url_alone_not_enough(self):
        decision = self._decide("Halo kak", url="https://www.facebook.com/messages/")
        assert decision.allowed is False
        assert "bukan layar" in decision.reason

    def test_escalation_text_has_no_pin(self):
        assert PIN not in pin_escalation_text()


class TestFill:
    def test_types_pin_without_returning_it(self):
        page = FakePage(text="Masukkan PIN pemulihan chat")
        ok, note = fill_pin(page, PIN)
        assert ok is True
        # diketik per karakter (seperti manusia), hasil akhirnya tetap PIN utuh
        assert "".join(page.typed) == PIN
        assert page.keyboard.keys == ["Enter"]
        assert PIN not in note

    def test_invalid_pin_not_typed(self):
        page = FakePage(text="Masukkan PIN pemulihan chat")
        ok, note = fill_pin(page, "12")
        assert ok is False
        assert page.typed == []
        assert "12" not in note

    def test_missing_box(self):
        page = FakePage(text="Masukkan PIN pemulihan chat", pin_box=False)
        ok, note = fill_pin(page, PIN)
        assert ok is False
        assert "kotak PIN" in note


class TestAdapterIntegration:
    def _adapter(self, pin=PIN, auto=True):
        return MessengerAdapter(profile_dir="/tmp/x", chat_pin=pin, pin_auto=auto)

    def test_fills_on_pin_screen(self):
        adapter = self._adapter()
        page = FakePage(text="Masukkan PIN pemulihan chat Anda")
        assert adapter._handle_pin(page, stage="daftar_chat") == "terisi"
        assert adapter.pin_attempts == 1
        assert adapter.pin_status.startswith("terisi")

    def test_no_pin_screen_does_nothing(self):
        adapter = self._adapter()
        page = FakePage(text="Daftar chat Messenger")
        assert adapter._handle_pin(page, stage="daftar_chat") == "tidak_diminta"
        assert adapter.pin_attempts == 0

    def test_checkpoint_returns_perlu_pin(self):
        adapter = self._adapter()
        page = FakePage(text="Masukkan PIN. Checkpoint: konfirmasi identitas")
        assert adapter._handle_pin(page, stage="daftar_chat") == "perlu_pin"
        assert page.typed == []

    def test_without_pin_configured(self):
        adapter = self._adapter(pin="")
        page = FakePage(text="Masukkan PIN pemulihan chat")
        assert adapter._handle_pin(page, stage="baca_chat") == "perlu_pin"
        assert page.typed == []
        assert "FBW_CHAT_PIN" in adapter.pin_status

    def test_attempts_limited(self):
        adapter = self._adapter()
        for _ in range(MAX_ATTEMPTS_PER_RUN):
            adapter._handle_pin(FakePage(text="Masukkan PIN pemulihan chat"), stage="baca_chat")
        page = FakePage(text="Masukkan PIN pemulihan chat")
        assert adapter._handle_pin(page, stage="baca_chat") == "perlu_pin"
        assert page.typed == []

    def test_pin_never_in_diagnostics(self):
        adapter = self._adapter()
        adapter._handle_pin(FakePage(text="Masukkan PIN pemulihan chat"), stage="daftar_chat")
        blob = str(adapter.last_diagnostics)
        assert PIN not in blob
        assert "•" in blob


class TestSettings:
    def test_pin_from_env(self, monkeypatch):
        from fbworkspace.config import get_settings

        monkeypatch.setenv("FBW_CHAT_PIN", "102002")
        assert get_settings().chat_pin == "102002"

    def test_pin_not_in_safety_summary(self, monkeypatch):
        from fbworkspace.config import get_settings

        monkeypatch.setenv("FBW_CHAT_PIN", "102002")
        summary = get_settings().describe_safety()
        assert PIN not in " ".join(summary.values())
        assert summary["pin_chat"].startswith("terisi")
