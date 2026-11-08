"""Worker balas DM Messenger.

Alur satu putaran:
    daftar chat -> baca isi -> pesan masuk terbaru -> susun balasan
    -> kirim (kalau aman) atau eskalasi ke WhatsApp Anda.

Pengaman:
- ``dry_run`` bawaannya True: semua analisis jalan, tapi tidak ada pesan/WA
  yang benar-benar dikirim.
- Maksimum balasan per putaran, jeda antar balasan, dan cooldown per chat.
- Harga hanya dari daftar harga; di luar template -> diam + eskalasi.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone

from .browser.messenger import MessengerAdapter, Thread
from .config import get_settings
from .db import Database
from .models import JobStatus
from .notify import Escalation, WhatsAppWebSender, build_login_escalation
from .ollama_client import OllamaConfig
from .replies import compose_reply
from .reply_style import polish

_LOG_LEVELS = {"info", "warning", "error"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counters_json(counters: dict) -> str:
    return json.dumps(counters, ensure_ascii=False)


def default_counters() -> dict[str, int]:
    return {
        "threads": 0,
        "messages": 0,
        "replies": 0,
        "silent": 0,
        "dry_run": 0,
        "escalations": 0,
        "notify_sent": 0,
        "notify_failed": 0,
        "skipped_cooldown": 0,
        "skipped_limit": 0,
        "errors": 0,
    }


def run(
    job_id: int,
    *,
    adapter=None,
    sender=None,
    database: Database | None = None,
    settings=None,
    sleep=time.sleep,
) -> int:
    settings = settings or get_settings()
    settings.ensure_dirs()
    db = database or Database(settings.db_path)

    job = db.get_job(job_id)
    if job is None:
        print(f"Job {job_id} tidak ditemukan.", file=sys.stderr)
        return 2

    config = job.get("config") or {}
    dry_run = bool(config.get("dry_run", settings.messenger_dry_run))
    max_threads = max(1, min(int(config.get("max_threads") or settings.messenger_max_threads), 20))
    max_replies = max(0, min(int(config.get("max_replies") or settings.messenger_max_replies), 30))
    gap = max(0, int(config.get("gap_seconds", settings.messenger_gap_seconds)))
    cooldown = max(0, int(config.get("cooldown_seconds", settings.messenger_cooldown_seconds)))
    notify_enabled = bool(config.get("notify", True))
    ai_style = bool(config.get("ai_style", settings.messenger_ai_style))
    style_config = (
        OllamaConfig.from_settings(settings)
        if ai_style and getattr(settings, "ollama_enabled", False)
        else None
    )

    counters = default_counters()
    entries = db.list_prices()

    db.update_job(
        job_id,
        status=JobStatus.RUNNING.value,
        started_at=job.get("started_at") or _now(),
        heartbeat_at=_now(),
        message="Worker balas DM dimulai.",
    )
    db.log_event(
        job_id,
        "info",
        f"Balas DM mulai. Uji kering={dry_run}, maks {max_threads} chat / {max_replies} balasan.",
    )

    def push() -> None:
        db.update_job(job_id, counters_json=_counters_json(counters), heartbeat_at=_now())

    def log(level: str, message: str) -> None:
        db.log_event(job_id, level if level in _LOG_LEVELS else "info", str(message)[:500])

    status = JobStatus.COMPLETED.value
    message = ""

    if adapter is None:
        adapter = MessengerAdapter(
            profile_dir=settings.browser_profile_dir,
            headless=settings.browser_headless,
            slow_mo_ms=settings.browser_slow_mo_ms,
        )
    available, why = adapter.is_available()
    if not available:
        db.update_job(
            job_id,
            status=JobStatus.FAILED.value,
            message=why,
            finished_at=_now(),
            counters_json=_counters_json(counters),
        )
        db.log_event(job_id, "error", why)
        return 1

    if sender is None and notify_enabled:
        sender = WhatsAppWebSender(
            profile_dir=settings.wa_profile_dir,
            phone=settings.wa_phone,
            headless=True,
        )

    try:
        threads, list_status = adapter.list_threads(limit=max_threads)
        if list_status == "perlu_login":
            esc = build_login_escalation()
            _escalate(db, job_id, None, esc, counters, sender, dry_run, log)
            status = JobStatus.BLOCKED.value
            message = "Facebook meminta login. Buka jendela login lalu jalankan ulang."
            db.log_event(job_id, "warning", message)
            threads = []
        elif list_status != "ok":
            log("warning", f"Daftar chat tidak terbaca: {list_status}")
        counters["threads"] = len(threads)
        push()

        for thread in threads:
            if db.is_stop_requested(job_id):
                status = JobStatus.STOPPED.value
                message = "Dihentikan oleh pengguna."
                break
            if counters["replies"] >= max_replies and max_replies > 0:
                counters["skipped_limit"] += 1
                continue

            chat = db.get_chat(thread.key)
            if chat and chat.get("last_reply_at") and cooldown:
                if _seconds_since(chat["last_reply_at"]) < cooldown:
                    counters["skipped_cooldown"] += 1
                    continue

            db.upsert_chat(thread.key, name=thread.name, url=thread.url)
            incoming_count = _handle_thread(
                db=db,
                adapter=adapter,
                sender=sender,
                thread=thread,
                job_id=job_id,
                counters=counters,
                dry_run=dry_run,
                gap=gap,
                max_replies=max_replies,
                log=log,
                sleep=sleep,
                entries=entries,
                style_config=style_config,
            )
            counters["messages"] += incoming_count
            push()

        if status == JobStatus.COMPLETED.value:
            message = (
                f"{counters['threads']} chat dibaca, {counters['replies']} balasan terkirim, "
                f"{counters['escalations']} eskalasi, {counters['errors']} error."
                + (" (uji kering: tidak ada yang dikirim)" if dry_run else "")
            )
    except KeyboardInterrupt:
        status = JobStatus.STOPPED.value
        message = "Worker dihentikan (Ctrl+C)."
    except Exception as exc:  # noqa: BLE001
        status = JobStatus.FAILED.value
        message = f"Worker gagal: {exc.__class__.__name__}: {exc}"
        db.log_event(job_id, "error", message)
        db.log_event(job_id, "error", traceback.format_exc()[:1500])
    finally:
        if db.is_stop_requested(job_id) and status == JobStatus.COMPLETED.value:
            status = JobStatus.STOPPED.value
            message = message or "Dihentikan oleh pengguna."
        db.update_job(
            job_id,
            status=status,
            message=message,
            finished_at=_now(),
            heartbeat_at=_now(),
            counters_json=_counters_json(counters),
        )
        db.log_event(job_id, "info", f"Job balas DM selesai: {status}. {message}")
        print(f"[messenger] job {job_id}: {status} - {message}")

    return 0 if status == JobStatus.COMPLETED.value else 1


def _handle_thread(
    *,
    db: Database,
    adapter,
    sender,
    thread: Thread,
    job_id: int,
    counters: dict,
    dry_run: bool,
    gap: int,
    max_replies: int,
    log,
    sleep,
    entries,
    style_config=None,
) -> int:
    """Baca & tanggapi satu percakapan. Kembalikan jumlah pesan masuk baru."""
    known_out = db.outgoing_texts(thread.key)
    read = adapter.read_thread(thread.url, known_out=known_out)
    if read.status != "ok":
        log("warning", f"[{thread.name}] gagal membaca chat: {read.status} {read.note}".strip())
        return 0

    new_messages = 0
    for msg in read.messages:
        if db.record_message(thread.key, msg.key, direction=msg.direction, text=msg.text):
            new_messages += 1

    pending = db.unanswered_incoming(thread.key)
    if not pending:
        counters["silent"] += 1
        return new_messages

    latest = pending[-1]
    plan = compose_reply(latest["text"], entries)
    keys = [row["msg_key"] for row in pending]
    sender_desc = "kirim" if plan.should_send else "diam"
    log(
        "info",
        f"[{thread.name}] {sender_desc}: {plan.reason or 'template'} | {latest['text'][:80]!r}",
    )

    sent_text = ""
    body = plan.body
    if plan.should_send and style_config is not None:
        styled = polish(body, config=style_config)
        if styled.changed:
            log("info", f"[{thread.name}] variasi AI: {styled.text[:120]}")
            body = styled.text
        elif styled.reason:
            log("info", f"[{thread.name}] variasi AI dilewati: {styled.reason}")

    if plan.should_send:
        if dry_run:
            counters["dry_run"] += 1
            log("info", f"[{thread.name}] uji kering, tidak dikirim: {body.replace(chr(10), ' / ')[:120]}")
        elif counters["replies"] >= max_replies:
            counters["skipped_limit"] += 1
        else:
            ok, note = adapter.send_message(thread.url, body)
            if ok:
                counters["replies"] += 1
                sent_text = body
                db.record_message(
                    thread.key, f"out:{latest['msg_key']}", direction="out", text=body
                )
                log("info", f"[{thread.name}] balasan terkirim: {body.replace(chr(10), ' / ')[:120]}")
                if gap:
                    sleep(gap)
            else:
                counters["errors"] += 1
                log("error", f"[{thread.name}] gagal mengirim: {note}")
                if note == "perlu_login":
                    raise RuntimeError("Facebook meminta login saat mengirim balasan.")

    if plan.escalate:
        _escalate(
            db,
            job_id,
            thread,
            Escalation(
                chat_name=thread.name,
                message=latest["text"],
                reason=plan.reason or "di luar template",
                url=thread.url,
                when=datetime.now().strftime("%d/%m %H:%M"),
            ),
            counters,
            sender,
            dry_run,
            log,
        )

    db.mark_answered_many(thread.key, keys, sent_text)
    return new_messages


def _escalate(db, job_id, thread, escalation: Escalation, counters, sender, dry_run, log) -> None:
    if thread is not None and db.escalation_exists(thread.url, escalation.message):
        return
    esc_id = db.add_escalation(
        chat_name=escalation.chat_name,
        message=escalation.message,
        reason=escalation.reason,
        chat_url=escalation.url,
        channel="uji-kering" if dry_run else "whatsapp-web",
    )
    counters["escalations"] += 1

    if dry_run:
        db.mark_escalation(esc_id, "antre", error="uji kering: tidak dikirim")
        log("info", f"Eskalasi disimpan (uji kering): {escalation.reason}")
        return

    if sender is None:
        log("warning", f"Eskalasi disimpan tanpa notifikasi: {escalation.reason}")
        return

    ok, note = sender.send(escalation.text())
    if ok:
        db.mark_escalation(esc_id, "terkirim")
        counters["notify_sent"] += 1
        log("info", "Notifikasi WhatsApp terkirim.")
    else:
        db.mark_escalation(esc_id, "gagal", error=note)
        counters["notify_failed"] += 1
        log("warning", f"Notifikasi WhatsApp gagal: {note}")


def _seconds_since(iso: str) -> float:
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return 1e9
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worker balas DM Messenger FB Workspace")
    parser.add_argument("--job-id", type=int, required=True)
    args = parser.parse_args(argv)
    return run(args.job_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
