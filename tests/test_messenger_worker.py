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

    def __init__(self, threads, messages, *, send_ok=True, list_status="ok", diagnostics=None):
        self.threads = threads
        self.messages = messages
        self.send_ok = send_ok
        self.list_status = list_status
        self.diagnostics = diagnostics if diagnostics is not None else {"url": "https://www.facebook.com/messages/"}
        self.sent: list[tuple[str, str]] = []

    def is_available(self):
        return True, ""

    def list_threads(self, limit: int = 8):
        return self.threads[:limit], self.list_status

    def list_threads_detailed(self, limit: int = 8):
        from fbworkspace.browser.messenger import ThreadList

        return ThreadList(
            threads=self.threads[:limit],
            status=self.list_status,
            diagnostics=self.diagnostics,
        )

    def read_thread(self, url: str, *, known_out=None, thread_name=""):
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
        # Tes tidak boleh ikut memantau terus-menerus (loop tak berujung).
        messenger_watch=False,
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
        "watch": False,
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
    # uji kering TIDAK menandai terjawab -> putaran berikutnya masih bisa mengirim
    assert len(db.unanswered_incoming(THREAD_KEY)) == 1


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


def test_cooldown_delays_new_question_and_says_why(tmp_path):
    """Cooldown tidak lagi membisukan chat: pesan baru dijawab setelah jeda.

    Kejadian nyata 8 Nov 21:00-21:04: worker membalas "Ready kak", lalu chat itu
    dilewati diam-diam selama 10 menit sehingga pesan "berapa" berikutnya tidak
    pernah dibaca (bahkan tidak muncul di log).
    """
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus", THREAD_URL)],
        {THREAD_URL: [_incoming("Iphone 8 ready?")]},
    )
    first = _job(db, dry_run=False)
    run(first, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(adapter.sent) == 1

    # pesan baru masuk, tetapi jeda minimal belum lewat
    adapter.messages[THREAD_URL] = [_UnreadMsg("m2", "in", "iphone 8 64gb berapa", unread=True)]
    second = _job(db, dry_run=False, cooldown_seconds=600)
    run(second, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert len(adapter.sent) == 1, "belum boleh kirim dalam masa jeda"
    assert db.get_job(second)["counters"]["skipped_cooldown"] == 1
    events = [e["message"] for e in db.get_events(second)]
    assert any("ditunda" in m and "jeda minimal" in m for m in events)
    # pesannya TETAP menunggu, jadi akan dijawab setelah jeda lewat
    assert [row["text"] for row in db.unanswered_incoming(THREAD_KEY)] == [
        "iphone 8 64gb berapa"
    ]


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
        "cycles",
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


def test_no_chat_read_is_reported_as_blocked_not_success(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger(
        [],
        {},
        list_status="tidak_ada_chat",
        diagnostics={
            "url": "https://www.facebook.com/messages/",
            "title": "Messenger",
            "thread_links": 0,
            "rows": 0,
            "composer": "",
            "login_form": False,
            "text": "Tidak ada chat",
        },
    )

    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "diblokir/tidak diizinkan"
    assert "tidak ada chat yang terbaca" in job["message"].lower()

    messages = [e["message"] for e in db.get_events(job_id)]
    assert any("Diagnostik:" in m for m in messages)
    assert any("tautan_chat=0" in m for m in messages)


def test_diagnostics_saved_on_job(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    diagnostics = {"url": "x", "thread_links": 3, "composer": "div", "title": "Inbox"}
    adapter = FakeMessenger([], {}, list_status="tidak_ada_chat", diagnostics=diagnostics)

    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert db.get_job(job_id)["diagnostics"]["thread_links"] == 3


def test_checkpoint_page_blocks_job(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger(
        [],
        {},
        list_status="pemblokiran",
        diagnostics={"url": "https://www.facebook.com/checkpoint/", "reason": "checkpoint", "title": "Security check"},
    )
    sender = MemorySender()

    code = run(job_id, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "diblokir/tidak diizinkan"
    assert "pembatasan" in job["message"]
    assert sender.sent and sender.sent[0].startswith(STOP_HEADER)


def test_launch_error_marks_job_failed(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([], {}, list_status="kesalahan", diagnostics={"error": "TimeoutError: 30000ms"})

    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "gagal"
    assert "TimeoutError" in job["message"]


def test_old_adapter_without_detailed_still_works(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)

    class OldAdapter:
        name = "lama"

        def is_available(self):
            return True, ""

        def list_threads(self, limit=8):
            return [Thread(THREAD_KEY, "Rian", THREAD_URL)], "ok"

        def read_thread(self, url, *, known_out=None, thread_name=""):
            return ThreadRead(messages=[_incoming("iphone 11 ready?")], status="ok")

        def send_message(self, url, text):
            return True, "terkirim"

    code = run(job_id, adapter=OldAdapter(), sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert code == 0
    assert db.get_job(job_id)["counters"]["replies"] == 1


def test_broken_thread_does_not_stop_job(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)

    class Exploding(FakeMessenger):
        def read_thread(self, url, *, known_out=None, thread_name=""):
            raise RuntimeError("halaman crash")

    adapter = Exploding([Thread(THREAD_KEY, "Rian", THREAD_URL)], {})
    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert code == 0
    assert db.get_job(job_id)["counters"]["errors"] == 1


def test_read_failure_note_is_logged(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)

    class Unreadable(FakeMessenger):
        def read_thread(self, url, *, known_out=None, thread_name=""):
            return ThreadRead(messages=[], status="kesalahan", note="TimeoutError: 20000ms")

    adapter = Unreadable([Thread(THREAD_KEY, "Rian", THREAD_URL)], {})
    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    assert code == 0
    assert db.get_job(job_id)["counters"]["errors"] == 1
    assert any("TimeoutError" in e["message"] for e in db.get_events(job_id))


def test_pin_required_blocks_job_and_notifies(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger(
        [],
        {},
        list_status="perlu_pin",
        diagnostics={"url": "https://www.facebook.com/messages/", "pin": "bukan layar permintaan PIN"},
    )
    sender = MemorySender()

    code = run(job_id, adapter=adapter, sender=sender, database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "diblokir/tidak diizinkan"
    assert "PIN" in job["message"]
    assert sender.sent and sender.sent[0].startswith(STOP_HEADER)
    assert "PIN pemulihan" in sender.sent[0]
    assert "102002" not in sender.sent[0]


def test_send_needs_pin_fails_job(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)

    class PinOnSend(FakeMessenger):
        def send_message(self, url, text):
            return False, "perlu_pin"

    adapter = PinOnSend([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("iphone 11 ready?")]})
    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "gagal"
    assert "PIN" in job["message"]


def test_dry_run_then_live_sends_the_same_message(tmp_path):
    """Bug nyata 8 Nov 17:58: uji kering menandai terjawab, putaran nyata jadi diam."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("Iphone 11 ready?")]},
    )
    settings = _settings(tmp_path)

    dry = _job(db, dry_run=True)
    run(dry, adapter=adapter, sender=MemorySender(), database=db, settings=settings, sleep=lambda s: None)
    assert adapter.sent == []

    live = _job(db, dry_run=False, cooldown_seconds=0)
    run(live, adapter=adapter, sender=MemorySender(), database=db, settings=settings, sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "Ready kak")]
    assert db.get_job(live)["counters"]["replies"] == 1


def test_live_run_after_dry_is_logged(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("harga iphone 11 64gb?")]},
    )
    settings = _settings(tmp_path)
    run(_job(db, dry_run=True), adapter=adapter, sender=MemorySender(), database=db,
        settings=settings, sleep=lambda s: None)
    live = _job(db, dry_run=False, cooldown_seconds=0)
    run(live, adapter=adapter, sender=MemorySender(), database=db, settings=settings,
        sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "2.400.000 kak")]


def test_no_pending_messages_is_logged(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: []})

    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    messages = [e["message"] for e in db.get_events(job_id)]
    assert any("tidak ada pesan masuk yang belum dijawab" in m for m in messages)
    assert any("0 pesan terbaca" in m for m in messages)


def test_dry_run_log_explains_not_marked(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=True)
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: [_incoming("iphone 11 ready?")]}
    )

    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    messages = [e["message"] for e in db.get_events(job_id)]
    assert any("belum ditandai terjawab karena uji kering" in m for m in messages)


def test_dm_notes_distinguish_reply_and_silence(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("iphone 11 ready?"), _incoming("ok makasih", key="m2")]},
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    rows = db.unanswered_incoming(THREAD_KEY)
    assert rows == []


class _StoppingMessenger(FakeMessenger):
    """Adapter yang meminta job berhenti setelah N putaran."""

    def __init__(self, *args, stop_after=2, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.stop_after = stop_after
        self.db = None
        self.job_id = None

    def list_threads_detailed(self, limit=8):
        self.calls += 1
        if self.calls > self.stop_after and self.db is not None:
            self.db.request_stop(self.job_id)
        return super().list_threads_detailed(limit=limit)


def test_watch_mode_runs_multiple_cycles_until_limit(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("iphone 11 ready?")]},
    )
    job_id = _job(db, dry_run=False, watch=True, interval_seconds=10, max_cycles=3)

    sleeps: list[int] = []
    code = run(
        job_id,
        adapter=adapter,
        sender=MemorySender(),
        database=db,
        settings=_settings(tmp_path),
        sleep=lambda s: sleeps.append(s),
    )

    assert code == 0
    job = db.get_job(job_id)
    assert job["counters"]["cycles"] == 3
    assert "Mode pantau selesai setelah 3 putaran" in job["message"]
    # tetap ada jeda antar putaran (bukan loop tanpa henti)
    assert sum(sleeps) >= 20


def test_watch_mode_stops_when_user_requests(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False, watch=True, interval_seconds=10)
    adapter = _StoppingMessenger(
        [Thread(THREAD_KEY, "Rian", THREAD_URL)],
        {THREAD_URL: [_incoming("iphone 11 ready?")]},
        stop_after=2,
    )
    adapter.db = db
    adapter.job_id = job_id

    code = run(job_id, adapter=adapter, sender=MemorySender(), database=db,
               settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "dihentikan"
    assert "Dihentikan oleh pengguna" in job["message"]


def test_watch_mode_stops_on_blocking_problem(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False, watch=True, interval_seconds=10)
    adapter = FakeMessenger([], {}, list_status="perlu_login")
    sender = MemorySender()

    code = run(job_id, adapter=adapter, sender=sender, database=db,
               settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert code == 1
    assert job["status"] == "diblokir/tidak diizinkan"
    # tidak berputar terus saat diblokir, dan pemilik diberi tahu sekali
    assert len(sender.sent) == 1


def test_single_cycle_is_default(tmp_path):
    db = Database(tmp_path / "chat.db")
    job_id = _job(db, dry_run=False)
    adapter = FakeMessenger([Thread(THREAD_KEY, "Rian", THREAD_URL)], {THREAD_URL: []})

    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    # satu putaran = 1 cycle; mode pantau tidak aktif
    assert db.get_job(job_id)["counters"]["cycles"] == 1


def test_earlier_question_still_answered_when_last_is_greeting(tmp_path):
    """Pembeli: "berapa ya" lalu "permisi kak" -> pertanyaan pertama tetap dijawab."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Bagas", THREAD_URL)],
        {
            THREAD_URL: [
                _incoming("ip 11 64gb berapa ya", key="q1"),
                _incoming("permisi kak", key="q2"),
            ]
        },
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "2.400.000 kak")]


class _PreviewThread(Thread):
    pass


def _fake_thread(**kwargs):
    from fbworkspace.browser.messenger import Thread as T

    return T(**kwargs)


def test_lost_question_is_reported_not_silent(tmp_path):
    """Chat ditandai belum dibaca tetapi tak ada pesan terbaca -> WA ke pemilik."""
    from fbworkspace.browser.messenger import Thread as T

    db = Database(tmp_path / "chat.db")
    thread = T(
        key=THREAD_KEY,
        name="Bagas Sunaryo",
        url=THREAD_URL,
        unread=True,
        preview="berapa ya",
    )
    adapter = FakeMessenger([], {}, list_status="ok")
    adapter.threads = [thread]

    job_id = _job(db, dry_run=False)
    sender = MemorySender()
    run(job_id, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert sender.sent, "pemilik harus diberi tahu"
    assert "berapa ya" in sender.sent[0]
    assert "Bagas Sunaryo" in sender.sent[0]


class _UnreadMsg:
    """ChatMessage dengan penanda belum dibaca dari Facebook."""

    def __init__(self, key, direction, text, unread=False):
        from fbworkspace.browser.messenger import ChatMessage

        self._msg = ChatMessage(key, direction, text, unread=unread)

    def __getattr__(self, item):
        return getattr(self._msg, item)


def test_unread_question_uses_chat_context(tmp_path):
    """Skenario nyata 8 Nov 19:17: 'Berapa ya?' setelah 'Iphone 11 ready?'."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {
            THREAD_URL: [
                _UnreadMsg("m1", "in", "Iphone 11 ready?"),
                _incoming("Ready kak", key="o1"),
                _UnreadMsg("m2", "in", "Berapa ya", unread=True),
            ]
        },
    )
    job_id = _job(db, dry_run=False)
    sender = MemorySender()
    run(job_id, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    # model diambil dari konteks: tanya storage, bukan mengarang harga
    assert adapter.sent == [(THREAD_URL, "Storage-nya 64GB, 128GB, atau 256GB kak?")]
    assert sender.sent == []


def test_unread_question_without_context_escalates(tmp_path):
    """Harga tanpa model dan tanpa konteks: tidak dikarang -> WA ke pemilik."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Bagas", THREAD_URL)],
        {THREAD_URL: [_UnreadMsg("m1", "in", "Berapa ya", unread=True)]},
    )
    job_id = _job(db, dry_run=False)
    sender = MemorySender()
    run(job_id, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == []
    assert sender.sent and "Berapa ya" in sender.sent[0]


def test_unread_question_with_model_is_answered(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {
            THREAD_URL: [
                _UnreadMsg("m1", "in", "ip 11 64gb berapa", unread=True),
            ]
        },
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "2.400.000 kak")]


def test_answered_but_never_replied_message_is_reopened(tmp_path):
    """Data lama: pesan ditandai terjawab padahal tak ada balasan -> dibuka lagi."""
    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="Pak Bagus Sunaryo", url=THREAD_URL)
    db.record_message(THREAD_KEY, "m1", direction="in", text="berapa ya")
    db.mark_answered_many(THREAD_KEY, ["m1"], "", note="diam")  # tak ada balasan

    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {THREAD_URL: [_UnreadMsg("m1", "in", "berapa ya", unread=True)]},
    )
    job_id = _job(db, dry_run=False)
    sender = MemorySender()
    run(job_id, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert sender.sent, "pemilik harus diberi tahu soal 'berapa ya'"
    events = [e["message"] for e in db.get_events(job_id)]
    assert any("dibuka kembali" in m for m in events)


def test_log_shows_what_was_read_not_only_count(tmp_path):
    """Log 'tidak ada pesan masuk' harus disertai isi terbaca agar bisa diperiksa."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {
            THREAD_URL: [
                _incoming("Iphone 11 ready?"),
                ChatMessage("o1", "out", "Ready kak"),
            ]
        },
    )
    adapter.last_debug = {
        "used": 2,
        "lines": ["in: Iphone 11 ready?", "out: Ready kak"],
        "our_side": "kiri",
    }
    # pesan sudah terjawab sebelumnya -> tidak ada kandidat, jalur log "tidak ada"
    db.upsert_chat(THREAD_KEY, name="Pak Bagus Sunaryo", url=THREAD_URL)
    db.record_message(THREAD_KEY, "m1", direction="in", text="Iphone 11 ready?")
    db.record_message(THREAD_KEY, "o1", direction="out", text="Ready kak")
    db.mark_answered_many(THREAD_KEY, ["m1"], "Ready kak")
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    events = [e["message"] for e in db.get_events(job_id)]
    assert any("isi terbaca" in m and "Ready kak" in m for m in events)


def test_storage_only_followup_gets_price_from_context(tmp_path):
    """Alur nyata: 'Iphone 11 ready?' -> kita tanya storage -> pembeli jawab '64gb'."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {
            THREAD_URL: [
                ChatMessage("m1", "in", "Iphone 11 ready?"),
                ChatMessage("o1", "out", "Ready kak"),
                ChatMessage("m2", "in", "Berapa ya"),
                ChatMessage("o2", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
                ChatMessage("m3", "in", "64gb", unread=True),
            ]
        },
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "2.400.000 kak")]


class _FlakySender:
    """WA gagal pada percobaan pertama (headless), berhasil saat jendela terlihat."""

    name = "flaky-wa"

    def __init__(self):
        self.calls = 0

    def send(self, text):
        self.calls += 1
        return False, "kotak tulis WhatsApp tidak ditemukan"

    def send_reliable(self, text, *, tries=3):
        self.calls += 1
        return True, "terkirim (berhasil pada percobaan 2)"


def test_worker_uses_send_reliable_for_escalations(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Bagas", THREAD_URL)],
        {THREAD_URL: [_incoming("berapa ya")]},
    )
    sender = _FlakySender()
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert sender.calls == 1
    assert db.get_job(job_id)["counters"]["notify_sent"] == 1
    assert db.get_job(job_id)["counters"]["notify_failed"] == 0


def test_failed_whatsapp_is_reported_in_summary(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Bagas", THREAD_URL)],
        {THREAD_URL: [_incoming("berapa ya")]},
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(ok=False, message="gagal"),
        database=db, settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    assert job["counters"]["notify_failed"] == 1
    assert "PERHATIAN" in job["message"]
    events = [e["message"] for e in db.get_events(job_id)]
    assert any("TIDAK menerima kabar" in m for m in events)


_FULL_CHAT = [
    ChatMessage("m1", "in", "Iphone 11 ready?"),
    ChatMessage("o1", "out", "Ready kak"),
    ChatMessage("m2", "in", "Berapa ya"),
    ChatMessage("o2", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
    ChatMessage("m3", "in", "128 kak", unread=True),
]


def _seed_stale_question(db):
    """Keadaan nyata: 'Berapa ya' masih tercatat belum dijawab padahal sudah dibalas."""
    db.upsert_chat(THREAD_KEY, name="Pak Bagus Sunaryo", url=THREAD_URL)
    for msg in _FULL_CHAT[:4]:
        db.record_message(THREAD_KEY, msg.key, direction=msg.direction, text=msg.text)
    db.record_message(
        THREAD_KEY, "o2b", direction="out", text="Storage-nya 64GB, 128GB, atau 256GB kak?"
    )
    db.mark_answered_many(
        THREAD_KEY, ["m2"], "Storage-nya 64GB, 128GB, atau 256GB kak?", note="dibalas"
    )


def test_old_question_is_not_asked_twice(tmp_path):
    """Bug 19:37-19:44: worker menanyakan storage lagi padahal jawabannya '128 kak'."""
    db = Database(tmp_path / "chat.db")
    _seed_stale_question(db)
    adapter = _MetaMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)], {THREAD_URL: _FULL_CHAT}
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "2.600.000 kak")]
    events = [e["message"] for e in db.get_events(job_id)]
    assert any("sudah ditanggapi balasan kita" in m for m in events)


def test_reply_closes_older_incoming_messages(tmp_path):
    """Pesan masuk sebelum balasan terakhir kita dianggap sudah ditanggapi."""
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.browser.messenger import ThreadRead
    from fbworkspace.messenger_worker import _close_answered_by_reply

    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="x", url=THREAD_URL)
    db.record_message(THREAD_KEY, "m1", direction="in", text="Berapa ya")
    db.record_message(THREAD_KEY, "o1", direction="out", text="Storage-nya 64 atau 128 kak?")
    db.mark_answered_many(THREAD_KEY, ["m1"], "", note="diam")  # belum ditutup

    read = ThreadRead(
        [
            CM("m1", "in", "Berapa ya"),
            CM("o1", "out", "Storage-nya 64 atau 128 kak?"),
            CM("m2", "in", "128 kak"),
        ],
        "ok",
        meta={"urutan": "dom"},
    )
    closed = _close_answered_by_reply(db, THREAD_KEY, read, ["Storage-nya 64 atau 128 kak?"])
    assert closed == 1
    assert [row["msg_key"] for row in db.unanswered_incoming(THREAD_KEY)] == []


def test_no_closing_without_matching_reply(tmp_path):
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.browser.messenger import ThreadRead
    from fbworkspace.messenger_worker import _close_answered_by_reply

    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="x", url=THREAD_URL)
    db.record_message(THREAD_KEY, "m1", direction="in", text="Berapa ya")

    read = ThreadRead([CM("m1", "in", "Berapa ya")], "ok")
    assert _close_answered_by_reply(db, THREAD_KEY, read, ["balasan lain"]) == 0
    assert len(db.unanswered_incoming(THREAD_KEY)) == 1


_REAL_ORDER_20_19 = [
    ChatMessage("a", "in", "Iphone 11 ready?"),
    ChatMessage("o1", "out", "Ready kak"),
    ChatMessage("o2", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
    ChatMessage("o3", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
    ChatMessage("o4", "out", "2.600.000 kak"),
    ChatMessage("b", "in", "Iphone 8 ready?"),
    ChatMessage("o5", "out", "coba"),
    ChatMessage("c", "in", "Berapa ya"),
    ChatMessage("d", "in", "128 kak"),
]


class _MetaMessenger(FakeMessenger):
    """Adapter yang melaporkan bukti urutan (seperti hasil bacaan halaman nyata)."""

    def read_thread(self, url, *, known_out=None, thread_name=""):
        from fbworkspace.browser.messenger import ThreadRead

        return ThreadRead(
            messages=self.messages.get(url, []),
            status="ok",
            meta={"our_side": "kanan", "urutan": "dom"},
        )


def test_real_order_still_answers_latest_message(tmp_path):
    """Laporan 20:19: 9 pesan terbaca, '128 kak' tetap harus ditanggapi."""
    db = Database(tmp_path / "chat.db")
    adapter = _MetaMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {THREAD_URL: _REAL_ORDER_20_19},
    )
    job_id = _job(db, dry_run=False)
    sender = MemorySender()
    run(job_id, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    job = db.get_job(job_id)
    # 128GB iPhone 8 tidak ada di daftar -> tidak dikarang, tetapi TIDAK diam:
    # pemilik diberi tahu lewat WA.
    assert adapter.sent == []
    assert sender.sent and "128 kak" in sender.sent[0]
    assert job["counters"]["escalations"] == 1


def test_incoming_after_our_reply_is_not_closed(tmp_path):
    """Pesan masuk sesudah balasan kita tidak boleh dianggap 'sudah ditanggapi'."""
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.browser.messenger import ThreadRead
    from fbworkspace.messenger_worker import _close_answered_by_reply

    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="x", url=THREAD_URL)
    db.record_message(THREAD_KEY, "m1", direction="in", text="Iphone 8 ready?")

    read = ThreadRead(
        [
            CM("m1", "in", "Iphone 8 ready?"),
            CM("o1", "out", "coba"),
        ],
        "ok",
        meta={"urutan": "dom"},
    )
    # "coba" bukan balasan template -> pesan pembeli tidak dianggap terjawab
    closed = _close_answered_by_reply(db, THREAD_KEY, read, ["coba"])
    assert closed == 1  # hanya pesan SEBELUM balasan (di sini tidak ada yang lain)
    assert db.unanswered_incoming(THREAD_KEY) == []


def test_context_ignores_unanswered_later_question(tmp_path):
    """'128 kak' harus memakai model 11, bukan 'Iphone 8 ready?' yang belum dijawab."""
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.browser.messenger import ThreadRead
    from fbworkspace.messenger_worker import _context_before

    read = ThreadRead(
        [
            CM("a", "in", "Iphone 11 ready?"),
            CM("b", "out", "Ready kak"),
            CM("c", "in", "Berapa ya"),
            CM("d", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
            CM("e", "in", "128 kak"),
            CM("f", "out", "2.600.000 kak"),
            CM("g", "in", "Iphone 8 ready?"),
        ],
        "ok",
    )
    context = _context_before(read, "128 kak")
    assert "Iphone 11 ready?" in context
    assert "Iphone 8 ready?" not in context


def test_unread_message_not_yet_parsed_is_recorded(tmp_path):
    """'Iphone 8 ready?' bertanda belum dibaca tetapi belum tercatat -> disimpan."""
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.browser.messenger import ThreadRead
    from fbworkspace.messenger_worker import _record_unread_messages

    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="Pak Bagus Sunaryo", url=THREAD_URL)
    thread = Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)
    read = ThreadRead([CM("g", "in", "Iphone 8 ready?", unread=True)], "ok")

    saved = _record_unread_messages(db, thread, read, log=lambda *a, **k: None)
    assert saved == 1
    assert [row["text"] for row in db.unanswered_incoming(THREAD_KEY)] == ["Iphone 8 ready?"]


def test_latest_question_answered_after_gap(tmp_path):
    """Alur lengkap: setelah 2.600.000, pembeli tanya 'Iphone 8 ready?' -> dijawab."""
    db = Database(tmp_path / "chat.db")
    adapter = _MetaMessenger(
        [Thread(THREAD_KEY, "Pak Bagus Sunaryo", THREAD_URL)],
        {
            THREAD_URL: [
                ChatMessage("m1", "in", "Iphone 11 ready?"),
                ChatMessage("o1", "out", "Ready kak"),
                ChatMessage("m2", "in", "Berapa ya"),
                ChatMessage("o2", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
                ChatMessage("m3", "in", "128 kak"),
                ChatMessage("o3", "out", "2.600.000 kak"),
                ChatMessage("m4", "in", "Iphone 8 ready?", unread=True),
            ]
        },
    )
    job_id = _job(db, dry_run=False)
    run(job_id, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert adapter.sent == [(THREAD_URL, "Ready kak")]


class _ReadMeta:
    def __init__(self, messages, meta=None):
        self.messages = messages
        self.meta = meta or {}


def test_closing_covers_only_the_answered_message(tmp_path):
    """Laporan 20:55: 'Iphone 8 ready?' ikut tertutup oleh balasan untuk 'Berapa ya?'."""
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.messenger_worker import _close_answered_by_reply

    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="x", url=THREAD_URL)
    for key, text in (("a", "Iphone 8 ready?"), ("b", "Berapa ya?")):
        db.record_message(THREAD_KEY, key, direction="in", text=text)

    read = _ReadMeta(
        [
            CM("a", "in", "Iphone 8 ready?"),
            CM("b", "in", "Berapa ya?"),
            CM("o", "out", "Storage-nya 64GB, 128GB, atau 256GB kak?"),
        ],
        {"urutan": "dom"},
    )
    closed = _close_answered_by_reply(db, THREAD_KEY, read, ["Storage-nya 64GB, 128GB, atau 256GB kak?"])

    assert closed == 1  # hanya "Berapa ya?" yang ditanggapi balasan itu
    assert [row["text"] for row in db.unanswered_incoming(THREAD_KEY)] == ["Iphone 8 ready?"]


def test_closing_does_not_guess_when_messages_have_no_order(tmp_path):
    """Tanpa bukti urutan, jangan menutup apa pun (lebih baik bertanya ulang)."""
    from fbworkspace.browser.messenger import ChatMessage as CM
    from fbworkspace.messenger_worker import _close_answered_by_reply

    db = Database(tmp_path / "chat.db")
    db.upsert_chat(THREAD_KEY, name="x", url=THREAD_URL)
    db.record_message(THREAD_KEY, "a", direction="in", text="Iphone 8 ready?")

    read = _ReadMeta(
        [CM("a", "in", "Iphone 8 ready?"), CM("o", "out", "Storage-nya 64 atau 128 kak?")]
    )
    assert _close_answered_by_reply(db, THREAD_KEY, read, ["Storage-nya 64 atau 128 kak?"]) == 0
    assert len(db.unanswered_incoming(THREAD_KEY)) == 1


def test_summary_mentions_deferred_chat(tmp_path):
    """Ringkasan putaran harus jelas soal chat yang ditunda, bukan terlihat kosong."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus", THREAD_URL)],
        {THREAD_URL: [_incoming("iphone 8 64gb berapa")]},
    )
    first = _job(db, dry_run=False)
    run(first, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    adapter.messages[THREAD_URL] = [
        _UnreadMsg("m2", "in", "iphone 8 64gb berapa", unread=True)
    ]
    second = _job(db, dry_run=False, cooldown_seconds=600)
    run(second, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert "ditunda karena jeda minimal" in db.get_job(second)["message"]


def test_two_questions_answered_across_cycles(tmp_path):
    """Dua pertanyaan menumpuk: dijawab satu per putaran, keduanya tidak ada yang hilang.

    Tangkapan layar 8 Nov 20:44: setelah "Ready kak" untuk "Iphone 8 ready?",
    muncul "Berapa ya?" lalu "Berapa" — keduanya belum dijawab.
    """
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus", THREAD_URL)],
        {
            THREAD_URL: [
                ChatMessage("o1", "out", "2.600.000 kak"),
                _UnreadMsg("m1", "in", "Iphone 8 ready?", unread=True),
                _UnreadMsg("m2", "in", "Berapa", unread=True),
            ]
        },
    )

    first = _job(db, dry_run=False, cooldown_seconds=0)
    run(first, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(adapter.sent) == 1
    assert "Storage-nya" in adapter.sent[0][1]

    # putaran berikutnya: pertanyaan yang belum terjawab tetap menunggu
    assert [row["text"] for row in db.unanswered_incoming(THREAD_KEY)] == ["Iphone 8 ready?"]

    second = _job(db, dry_run=False, cooldown_seconds=0)
    run(second, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert [body for _, body in adapter.sent] == [
        adapter.sent[0][1],
        "Ready kak",
    ]
    assert db.unanswered_incoming(THREAD_KEY) == []


class _FailingSender:
    """Pengirim WA yang selalu gagal (meniru headless ditolak di mesin pengguna)."""

    def __init__(self):
        self.calls = 0

    def send_reliable(self, text):
        self.calls += 1
        return False, "WhatsApp Web menolak versi Chrome bawaan Playwright"

    def send(self, text):  # pragma: no cover - pengaman bila dipanggil
        return False, "gagal"


def test_escalation_failure_is_retried_on_next_cycle(tmp_path):
    """Laporan 21:06: 'Ada apa aja emang?' di luar template, WA tidak pernah masuk."""
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus", THREAD_URL)],
        {THREAD_URL: [_UnreadMsg("m1", "in", "Ada apa aja emang?", unread=True)]},
    )
    sender = _FailingSender()

    first = _job(db, dry_run=False)
    run(first, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert adapter.sent == [], "di luar template: worker harus diam"
    assert sender.calls == 1
    rows = db.list_escalations()
    assert len(rows) == 1
    assert rows[0]["status"] == "gagal"

    # putaran berikutnya: HARUS dicoba lagi (dulu ditelan dedup selamanya)
    second = _job(db, dry_run=False)
    run(second, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert sender.calls == 2
    rows = db.list_escalations()
    assert len(rows) == 1, "jangan membuat baris baru, kirim ulang yang sama"
    assert rows[0]["status"] == "gagal"
    assert rows[0]["attempts"] >= 2


def test_successful_escalation_is_not_repeated(tmp_path):
    db = Database(tmp_path / "chat.db")
    adapter = FakeMessenger(
        [Thread(THREAD_KEY, "Pak Bagus", THREAD_URL)],
        {THREAD_URL: [_UnreadMsg("m1", "in", "Ada apa aja emang?", unread=True)]},
    )
    sender = MemorySender()

    first = _job(db, dry_run=False)
    run(first, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(sender.sent) == 1

    second = _job(db, dry_run=False)
    run(second, adapter=adapter, sender=sender, database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(sender.sent) == 1, "notifikasi yang sudah terkirim tidak diulang"


class _GrowingMessenger(FakeMessenger):
    """Adapter yang menambah pesan pembeli di antara putaran (seperti chat nyata)."""

    def read_thread(self, url, *, known_out=None, thread_name=""):
        from fbworkspace.browser.messenger import ThreadRead

        return ThreadRead(
            messages=list(self.messages.get(url, [])),
            status="ok",
            meta={"our_side": "kanan", "urutan": "dom"},
        )


def test_reply_not_duplicated_when_new_message_arrives(tmp_path):
    """Bukti 21:1x: 'Storage-nya …' terkirim dua kali setelah 'Ada apa aja emang?'.

    Penyebab: kunci pesan memakai nomor urut di halaman, sehingga pesan lama
    bergeser dan dianggap baru ketika pesan pembeli bertambah.
    """
    db = Database(tmp_path / "chat.db")
    adapter = _GrowingMessenger(
        [Thread(THREAD_KEY, "Pak Bagus", THREAD_URL)],
        {THREAD_URL: [_UnreadMsg("x", "in", "iphone 8 64gb berapa", unread=True)]},
    )

    first = _job(db, dry_run=False, cooldown_seconds=0)
    run(first, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)
    assert len(adapter.sent) == 1

    # pembeli mengirim pesan baru: balasan kita + pesan itu kini ada di halaman
    adapter.messages[THREAD_URL] = [
        ChatMessage("m1", "in", "iphone 8 64gb berapa"),
        ChatMessage("o1", "out", adapter.sent[0][1]),
        ChatMessage("m2", "in", "Ada apa aja emang?"),
    ]
    second = _job(db, dry_run=False, cooldown_seconds=0)
    run(second, adapter=adapter, sender=MemorySender(), database=db,
        settings=_settings(tmp_path), sleep=lambda s: None)

    assert len(adapter.sent) == 1, "balasan yang sama tidak boleh dikirim ulang"
    events = [e["message"] for e in db.get_events(second)]
    # Pesan lama tidak diklaim "sudah ditanggapi" dua kali...
    assert sum("sudah ditanggapi balasan kita" in m for m in events) == 1
    # ...dan pertanyaan di luar template dilaporkan lewat WA, bukan diabaikan.
    assert [row["message"] for row in db.list_escalations()] == ["Ada apa aja emang?"]
