"""Adapter baca/kirim DM Facebook (Messenger) lewat browser yang sudah login.

Batas yang sama seperti adapter feed:
- tidak menembus CAPTCHA/challenge, tidak menyamarkan otomasi;
- berhenti dan melaporkan status bila diminta login;
- hanya membalas percakapan yang SUDAH ada (tidak menyapa orang baru).

Selector Facebook sering berubah dan BELUM diuji pada akun nyata. Karena itu
semua kegagalan dikembalikan sebagai status + pesan, bukan exception diam-diam,
dan ada ``diagnostics()`` untuk membantu Anda melaporkan apa yang terlihat.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

INBOX_URL = "https://www.facebook.com/messages/"
THREAD_LINK_SELECTOR = 'a[href*="/messages/t/"]'
MESSAGE_ROW_SELECTOR = 'div[role="row"]'
MESSAGE_TEXT_SELECTOR = 'div[dir="auto"]'
COMPOSER_SELECTOR = 'div[contenteditable="true"][role="textbox"]'
LOGIN_MARKERS = ('input[name="email"]', "#email", 'form[action*="login"]')


@dataclass(frozen=True)
class Thread:
    """Satu percakapan di daftar inbox."""

    key: str
    name: str
    url: str
    unread: bool = False


@dataclass
class ChatMessage:
    key: str
    direction: str  # "in" | "out"
    text: str
    author: str = ""


@dataclass
class ThreadRead:
    messages: list[ChatMessage] = field(default_factory=list)
    status: str = "ok"
    note: str = ""


def _text_of(element) -> str:
    try:
        return (element.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _msg_key(thread_key: str, index: int, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:10]
    return f"{thread_key}:{index}:{digest}"


def _direction(row, text: str, known_out: list[str]) -> str:
    """Tentukan arah pesan: 'out' (kita) atau 'in' (pembeli)."""
    for attr in ("aria-label", "data-scope"):
        try:
            label = (row.get_attribute(attr) or "").lower()
        except Exception:  # noqa: BLE001
            label = ""
        if not label:
            continue
        if "anda kirim" in label or "you sent" in label or "sent by you" in label:
            return "out"
        if "diterima" in label or "received" in label:
            return "in"
    try:
        marker = row.query_selector(
            '[aria-label*="Anda kirim"], [aria-label*="You sent"], '
            '[aria-label*="Anda mengirim"], [aria-label*="Sent by you"]'
        )
        if marker is not None:
            return "out"
    except Exception:  # noqa: BLE001
        pass
    normalized = " ".join(text.lower().split())
    for sent in known_out:
        if sent and normalized and normalized[:60] == " ".join(sent.lower().split())[:60]:
            return "out"
    return "in"


class MessengerAdapter:
    name = "messenger"

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        headless: bool = False,
        slow_mo_ms: int = 120,
        timeout_ms: int = 45_000,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms
        self.last_diagnostics: dict = {}

    # ------------------------------------------------------------------ util
    def is_available(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"Playwright tidak tersedia: {exc.__class__.__name__}"
        return True, ""

    def _launch(self):
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        return sync_playwright().start()

    def _context(self, playwright):
        return playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
        )

    def _login_required(self, page) -> bool:
        for selector in LOGIN_MARKERS:
            try:
                if page.query_selector(selector) is not None:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # ------------------------------------------------------------- daftar chat
    def list_threads(self, limit: int = 8) -> tuple[list[Thread], str]:
        playwright = self._launch()
        context = None
        try:
            context = self._context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if self._login_required(page):
                self.last_diagnostics = {"status": "perlu_login"}
                return [], "perlu_login"
            try:
                page.wait_for_selector(THREAD_LINK_SELECTOR, timeout=self.timeout_ms // 3)
            except Exception:  # noqa: BLE001
                self.last_diagnostics = {"status": "kosong", "url": page.url}
                return [], "tidak_ada_chat"

            anchors = page.query_selector_all(THREAD_LINK_SELECTOR)
            threads: list[Thread] = []
            seen: set[str] = set()
            for anchor in anchors:
                href = anchor.get_attribute("href") or ""
                if "/messages/t/" not in href:
                    continue
                key = href.split("/messages/t/")[1].split("/")[0].split("?")[0]
                if not key or key in seen:
                    continue
                seen.add(key)
                url = href if href.startswith("http") else f"https://www.facebook.com{href}"
                label = (anchor.get_attribute("aria-label") or "").strip()
                name = label or _text_of(anchor).split("\n")[0][:80]
                unread = "belum dibaca" in label.lower() or "unread" in label.lower()
                threads.append(Thread(key=key, name=name or key, url=url, unread=unread))
                if len(threads) >= max(1, limit):
                    break
            self.last_diagnostics = {"status": "ok", "threads": len(threads)}
            return threads, "ok"
        except Exception as exc:  # noqa: BLE001
            self.last_diagnostics = {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"[:300]}
            return [], "kesalahan"
        finally:
            if context is not None:
                context.close()
            playwright.stop()

    # ------------------------------------------------------------- baca isi chat
    def read_thread(self, url: str, *, known_out: list[str] | None = None) -> ThreadRead:
        thread_key = url.split("/messages/t/")[1].split("/")[0].split("?")[0] if "/messages/t/" in url else url
        playwright = self._launch()
        context = None
        try:
            context = self._context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if self._login_required(page):
                return ThreadRead(status="perlu_login")
            try:
                page.wait_for_selector(MESSAGE_ROW_SELECTOR, timeout=self.timeout_ms // 2)
            except Exception:  # noqa: BLE001
                return ThreadRead(status="tidak_ada_pesan")

            rows = page.query_selector_all(MESSAGE_ROW_SELECTOR)
            messages: list[ChatMessage] = []
            for index, row in enumerate(rows):
                text = ""
                try:
                    nodes = row.query_selector_all(MESSAGE_TEXT_SELECTOR)
                    if nodes:
                        text = "\n".join(_text_of(node) for node in nodes if _text_of(node))
                except Exception:  # noqa: BLE001
                    text = ""
                if not text:
                    text = _text_of(row)
                text = text.strip()
                if not text:
                    continue
                messages.append(
                    ChatMessage(
                        key=_msg_key(thread_key, index, text),
                        direction=_direction(row, text, known_out or []),
                        text=text[:2000],
                    )
                )
            return ThreadRead(messages=messages, status="ok", note=f"{len(rows)} baris")
        except Exception as exc:  # noqa: BLE001
            return ThreadRead(status="kesalahan", note=f"{exc.__class__.__name__}: {exc}"[:300])
        finally:
            if context is not None:
                context.close()
            playwright.stop()

    # ------------------------------------------------------------------ kirim
    def send_message(self, url: str, text: str) -> tuple[bool, str]:
        if not text.strip():
            return False, "teks kosong"
        playwright = self._launch()
        context = None
        try:
            context = self._context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if self._login_required(page):
                return False, "perlu_login"
            composer = page.wait_for_selector(COMPOSER_SELECTOR, timeout=self.timeout_ms)
            composer.click()
            page.keyboard.type(text, delay=25)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            needle = " ".join(text.split())[:40]
            if needle and needle.lower() in " ".join(page.inner_text("body").split()).lower():
                return True, "terkirim"
            return False, "tidak terverifikasi terkirim"
        except Exception as exc:  # noqa: BLE001
            return False, f"{exc.__class__.__name__}: {str(exc)[:200]}"
        finally:
            if context is not None:
                context.close()
            playwright.stop()
