"""Uji worker balas DM dengan adapter & pengirim palsu (tanpa browser)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fbworkspace.browser.messenger import ChatMessage, Thread, ThreadRead
from fbworkspace.config import Settings
from fbworkspace.db import Database
from fbworkspace.messenger_worker import default_counters, run
from fbworkspace.notify import HEADER, STOP_HEADER, MemorySender
from fbworkspace.reply_style import StyleResult

THREAD_URL = "https://www.facebook.com/messages/t/1001"
THREAD_KEY = "1001"


class FakeMessenger:
    name = "fake-messenger"

    def __init__(self, threads, messages, *, send_ok=True, list_status="ok"):
        self.threads = threads
        self.messages = messages
        self.send_ok = send_ok
        self.list_status = list_status
        self.sent: list[tuple[str, str]] = []

    def is_available(self):
        return True, ""

    def list_threads(self, limit: int = 8):
        return self.threads[:limit], self.list_status

    def read_thread(self, url: str, *, known_out=None):
        return ThreadRead(messages=self.messages.get(url, []), status="ok")

    def send_message(self, url: str, text: str):
        if not self.send_ok:
            return False, "gagal uji"
        self.sent.append((url, text))
        return True, "terkirim"


def _settings(tmp_path: Path) -> Settings:
    return replace(
        Settings(),
        db_path=tmp_path / "chat.db",
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        wa_profile_dir=tmp_path / "wa_profile",
        browser_profile_dir=tmp_path / "browser",
        messenger_gap_seconds=0,
        messenger_cooldown_seconds=0,
        messenger_dry_run=True,
    )


def _job(db: Database, **config) -> int:
    base = {
        "mode": "messenger",
        "dry_run": True,
        "max_threads": 5,
        "max_replies": 5,
        "gap_seconds": 0,
        "cooldown_seconds": 0,
        "notify": True,
    }
    base.update(config)
    return db.create_job(kind="messenger", group_urls=[], config=base)


def _incoming(text: str, key: str = "m1") -> ChatMessage:
    return ChatMessage(key=key, direction="in", text=text)


def test_dry_run_does_not_send_but_records(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db)
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("harga iphone 11 64gb?")]})
    sender = MemorySender()

    code = run(job_id, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert code == 0
    job = db.get_job(job_id)
    assert job["status"] == "selesai"
    assert job["counters"]["dry_run"] == 1
    assert job["counters"]["replies"] == 0
    assert adapter.sent == []
    assert sender.sent == []
    assert db.unanswered_incoming(THREAD_KEY) == []


def test_live_reply_uses_pricelist(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("harga iphone 11 64gb?")]})

    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert code == 0
    assert adapter.sent == [(THREAD_URL, "2.400.000 kak")]
    job = db.get_job(job_id)
    assert job["counters"]["replies"] == 1
    outgoing = db.outgoing_texts(THREAD_KEY)
    assert outgoing == ["2.400.000 kak"]


def test_nego_is_escalated_to_whatsapp(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("bisa nego kak?")]})
    sender = MemorySender()

    run(job_id, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == []
    assert len(sender.sent) == 1
    assert sender.sent[0].startswith(HEADER)
    assert "nego" in sender.sent[0]
    rows = db.list_escalations()
    assert len(rows) == 1
    assert rows[0]["status"] == "terkirim"
    assert rows[0]["chat_name"] == "Rian"


def test_escalation_not_duplicated(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("bisa nego kak?")]})
    sender = MemorySender()

    first = _job(db, dry_run=False)
    run(first, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)
    second = _job(db, dry_run=False)
    run(second, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert len(db.list_escalations()) == 1
    assert len(sender.sent) == 1


def test_cooldown_skips_same_chat(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("harga iphone 8 64gb?")]})

    first = _job(db, dry_run=False)
    run(first, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(adapter.sent) == 1

    second = _job(db, dry_run=False, cooldown_seconds=600)
    run(second, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(adapter.sent) == 1
    assert db.get_job(second)["counters"]["skipped_cooldown"] == 1


def test_reply_limit_enforced(tmp_path):
    db = Database(tmp_path / "chat.db")
    other_url = "https://www.facebook.com/messages/t/2002"
    threads = [Thread(THREAD_KEY, "A", THREAD_URL), Thread("2002", "B", other_url)]
    messages = {
        THREAD_URL: [_incoming("iphone 11 ready?")],
        other_url: [ChatMessage(key="m2", direction="in", text="iphone 8 ready?")],
    }
    adapter = FakeMessenger(threads, messages)
    job_id = _job(db, dry_run=False, max_replies=1)

    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert len(adapter.sent) == 1
    assert db.get_job(job_id)["counters"]["skipped_limit"] >= 1


def test_login_required_blocks_job(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([], {}, list_status="perlu_login")
    sender = MemorySender()

    code = run(job_id, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert code == 1
    assert db.get_job(job_id)["status"] == "diblokir/tidak diizinkan"
    assert sender.sent and sender.sent[0].startswith(STOP_HEADER)


def test_send_failure_counted(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("iphone 11 ready?")]},
        send_ok=False,
    )

    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    counters = db.get_job(job_id)["counters"]
    assert counters["errors"] == 1
    assert counters["replies"] == 0


def test_greeting_is_silent_without_escalation(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("ok makasih kak")]})
    sender = MemorySender()

    run(job_id, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == []
    assert sender.sent == []
    assert db.list_escalations() == []


def test_ai_style_applied_only_when_enabled(tmp_path, monkeypatch):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("harga iphone 11 64gb?")]},
    )
    calls = []

    def fake_polish(body, config=None):
        calls.append(body)
        return StyleResult("2.400.000 ya kak, siap", True, "variasi AI")

    monkeypatch.setattr("fbworkspace.messenger_worker.polish", fake_polish)

    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    # Ollama mati -> variasi AI tidak dipanggil sama sekali
    assert calls == []
    assert adapter.sent == [(THREAD_URL, "2.400.000 kak")]

    # pesan baru di chat yang sama (percakapan lanjut)
    adapter.messages[THREAD_URL] = [_incoming("harga iphone 11 64gb?", key="m2")]
    settings = replace(_settings(tmp_path), ollama_enabled=True)
    second = _job(db, dry_run=False, cooldown_seconds=0)
    run(second, adapter=adapter, sender=MemorySender(), database=db, settings=settings, sleep=lambda s: None)

    assert calls == ["2.400.000 kak"]
    assert adapter.sent[-1] == (THREAD_URL, "2.400.000 ya kak, siap")


def test_default_counters_complete():
    expected = {
        "threads", "messages", "replies", "silent", "dry_run", "escalations",
        "notify_sent", "notify_failed", "skipped_cooldown", "skipped_limit", "errors",
    }
    assert set(default_counters()) == expected


def test_worker_without_adapter_reports_missing_playwright(tmp_path, monkeypatch):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db)

    class Unavailable:
        name = "unavailable"

        def is_available(self):
            return False, "Playwright tidak tersedia"

    code = run(job_id, adapter=Unavailable(), sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)
    assert code == 1
    assert db.get_job(job_id)["status"] == "gagal"
