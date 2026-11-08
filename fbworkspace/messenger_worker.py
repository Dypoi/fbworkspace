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
from .chat_pin import pin_escalation_text
from .config import get_settings
from .db import Database
from .models import JobStatus
from .notify import STOP_HEADER, Escalation, WhatsAppWebSender, build_login_escalation
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
        "cycles": 0,
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
    # Mode pantau: ulangi putaran terus-menerus sampai pengguna menekan berhenti.
    watch = bool(config.get("watch", settings.messenger_watch))
    interval = max(
        10,
        int(config.get("interval_seconds") or settings.messenger_interval_seconds),
    )
    max_cycles = max(0, int(config.get("max_cycles") or 0))  # 0 = tanpa batas
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
            # Worker balas DM berjalan TANPA jendela: tidak mengganggu Chrome Anda
            # dan tidak berebut fokus. Untuk melihat halaman, pakai --show.
            headless=True,
            slow_mo_ms=1,
            chat_pin=getattr(settings, "chat_pin", ""),
            pin_auto=getattr(settings, "chat_pin_auto", True),
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
        cycle = 0
        while True:
            cycle += 1
            if db.is_stop_requested(job_id):
                status = JobStatus.STOPPED.value
                message = "Dihentikan oleh pengguna."
                break

            outcome, detail = _watch_cycle(
                db=db,
                adapter=adapter,
                sender=sender,
                job_id=job_id,
                counters=counters,
                entries=entries,
                dry_run=dry_run,
                max_threads=max_threads,
                max_replies=max_replies,
                gap=gap,
                cooldown=cooldown,
                style_config=style_config,
                log=log,
                sleep=sleep,
                push=push,
            )
            if outcome in {"blocked", "failed"}:
                status = (
                    JobStatus.BLOCKED.value if outcome == "blocked" else JobStatus.FAILED.value
                )
                message = detail
                break

            counters["cycles"] = cycle
            push()

            if not watch:
                break
            if max_cycles and cycle >= max_cycles:
                message = f"Mode pantau selesai setelah {cycle} putaran (batas yang Anda set)."
                break
            log(
                "info",
                f"Putaran {cycle} selesai. Menunggu {interval} detik sebelum memeriksa lagi "
                "(tekan ⛔ Hentikan untuk berhenti).",
            )
            waited = 0
            while waited < interval:
                if db.is_stop_requested(job_id):
                    break
                step = min(2, interval - waited)
                sleep(step)
                waited += step
                db.update_job(job_id, heartbeat_at=_now())
            if db.is_stop_requested(job_id):
                status = JobStatus.STOPPED.value
                message = "Dihentikan oleh pengguna."
                break

        if status == JobStatus.COMPLETED.value:
            summary = (
                f"{counters['threads']} chat dibaca, {counters['replies']} balasan terkirim, "
                f"{counters['escalations']} eskalasi, {counters['errors']} error."
                + (" (uji kering: tidak ada yang dikirim)" if dry_run else "")
            )
            # Mode pantau: pertahankan keterangan jumlah putaran, tambahkan ringkasan.
            message = f"{message} {summary}".strip() if message else summary
        elif status == JobStatus.BLOCKED.value and not message:
            message = "Worker berhenti sebelum membaca chat; lihat log di bawah."
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


def _watch_cycle(
    *,
    db: Database,
    adapter,
    sender,
    job_id: int,
    counters: dict,
    entries,
    dry_run: bool,
    max_threads: int,
    max_replies: int,
    gap: int,
    cooldown: int,
    style_config,
    log,
    sleep,
    push,
) -> tuple[str, str]:
    """Satu putaran pemantauan. Kembalikan (hasil, keterangan).

    hasil: ``ok`` (lanjut), ``blocked`` (perlu tindakan Anda), ``failed`` (error).
    """
    threads, list_status, diagnostics = _list_threads(adapter, max_threads)
    counters["threads"] = len(threads)
    message = ""
    if diagnostics:
        db.update_job(job_id, diagnostics_json=json.dumps(diagnostics, ensure_ascii=False))

    if list_status == "perlu_login":
        esc = build_login_escalation()
        _escalate(db, job_id, None, esc, counters, sender, dry_run, log)
        message = (
            "Facebook meminta login. Jalankan scripts\\login_helper.py, login sendiri, "
            "lalu jalankan ulang."
        )
        log("warning", message)
        return "blocked", message
    if list_status == "pemblokiran":
        esc = build_login_escalation()
        _escalate(db, job_id, None, esc, counters, sender, dry_run, log)
        detail = diagnostics.get("reason") or diagnostics.get("title") or "halaman pembatasan"
        message = (
            f"Facebook menampilkan halaman pembatasan ({detail}). "
            "Buka Facebook sendiri, selesaikan di sana, lalu jalankan ulang."
        )
        log("warning", message)
        return "blocked", message
    if list_status == "perlu_pin":
        esc = Escalation(
            chat_name="",
            message=pin_escalation_text(),
            reason="Messenger meminta PIN pemulihan chat",
            header=STOP_HEADER,
        )
        _escalate(db, job_id, None, esc, counters, sender, dry_run, log)
        message = (
            "Messenger meminta PIN pemulihan chat dan worker tidak mengisinya. "
            "Isi PIN sendiri di jendela browser, atau set FBW_CHAT_PIN di .env."
        )
        log("warning", message)
        return "blocked", message
    if list_status == "kesalahan":
        message = "Gagal membuka Messenger: " + str(diagnostics.get("error") or "tidak diketahui")
        log("error", message)
        return "failed", message
    if list_status != "ok":
        detail = _diagnostics_line(diagnostics)
        message = (
            "Tidak ada chat yang terbaca di Messenger. Kemungkinan: belum login di jendela "
            "browser yang dipakai worker, atau susunan halaman Facebook berubah."
        )
        log("warning", f"{message} Diagnostik: {detail}")
        return "blocked", message

    db.update_job(job_id, diagnostics_json=json.dumps(diagnostics, ensure_ascii=False))
    push()

    for thread in threads:
        if db.is_stop_requested(job_id):
            return "ok", "stop"
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
    return "ok", ""


def _list_threads(adapter, limit: int):
    """Ambil daftar chat + diagnostik, dengan adapter lama tetap didukung."""
    detailed = getattr(adapter, "list_threads_detailed", None)
    if callable(detailed):
        result = detailed(limit=limit)
        return result.threads, result.status, dict(result.diagnostics or {})
    threads, status = adapter.list_threads(limit=limit)
    return threads, status, dict(getattr(adapter, "last_diagnostics", {}) or {})


def _diagnostics_line(diagnostics: dict) -> str:
    """Ringkasan pendek apa yang terlihat di halaman (untuk log/dashboard)."""
    if not diagnostics:
        return "(tidak ada diagnostik)"
    parts = []
    url = str(diagnostics.get("url") or "")[:120]
    if url:
        parts.append(f"url={url}")
    title = str(diagnostics.get("title") or "")[:60]
    if title:
        parts.append(f"judul={title!r}")
    parts.append(f"tautan_chat={diagnostics.get('thread_links', 0)}")
    parts.append(f"baris={diagnostics.get('rows', 0)}")
    parts.append(f"kotak_tulis={'ada' if diagnostics.get('composer') else 'tidak ada'}")
    if diagnostics.get("login_form"):
        parts.append("form_login=ada")
    text = str(diagnostics.get("text") or "")[:140]
    if text:
        parts.append(f"teks={text!r}")
    if diagnostics.get("error"):
        parts.append(f"error={diagnostics['error']}")
    return " | ".join(parts)


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
    try:
        read = adapter.read_thread(thread.url, known_out=known_out, thread_name=thread.name)
    except Exception as exc:  # noqa: BLE001 - satu chat rusak tidak boleh menghentikan job
        counters["errors"] += 1
        log("error", f"[{thread.name}] gagal membuka chat: {exc.__class__.__name__}: {exc}"[:300])
        return 0
    if read.status == "perlu_pin":
        counters["errors"] += 1
        log("warning", f"[{thread.name}] {read.note or 'perlu PIN pemulihan chat'}"[:300])
        return 0
    if read.status != "ok":
        note = f" ({read.note})" if read.note else ""
        counters["errors"] += 1
        log("warning", f"[{thread.name}] gagal membaca chat: {read.status}{note}"[:300])
        debug = (getattr(adapter, "last_diagnostics", {}) or {}).get("debug") or {}
        if debug.get("dropped"):
            log("info", f"[{thread.name}] contoh baris yang dilewati: " + " ; ".join(debug["dropped"][:4])[:400])
        return 0

    new_messages = 0
    # Sebagian pesan bisa berubah arah setelah pembacaan ulang (mis. dulu dianggap
    # pesan kita karena posisi gelembung). Perbaiki, jangan biarkan tertinggal.
    flipped = 0
    for msg in read.messages:
        if db.record_message(thread.key, msg.key, direction=msg.direction, text=msg.text):
            new_messages += 1
        elif db.fix_message_direction(thread.key, msg.key, direction=msg.direction):
            flipped += 1
    if flipped:
        log(
            "info",
            f"[{thread.name}] {flipped} pesan diperbaiki arahnya "
            "(sebelumnya teranggap pesan kita)",
        )

    pending = db.unanswered_incoming(thread.key)
    if not pending:
        # Jangan percaya buta pada catatan sendiri: bila Facebook menandai pesan
        # belum dibaca, buka kembali pesan itu untuk dijawab.
        _heal_unanswered(db, job_id, thread, read, counters, sender, dry_run, log)
        pending = db.unanswered_incoming(thread.key)
    if not pending:
        counters["silent"] += 1
        preview = ""
        if read.messages:
            last = read.messages[-1]
            preview = f" | terakhir [{last.direction}]: {last.text[:60]!r}"
        log(
            "info",
            f"[{thread.name}] tidak ada pesan masuk yang belum dijawab "
            f"({len(read.messages)} pesan terbaca di percakapan){preview}",
        )
        # Jangan biarkan pertanyaan pembeli hilang tanpa jejak: kalau daftar chat
        # menandai belum dibaca (atau cuplikannya berbunyi seperti pertanyaan),
        # beri tahu pemilik alih-alih diam.
        _maybe_flag_lost_question(db, job_id, thread, counters, sender, dry_run, log)
        return new_messages

    target = _pick_target(read, pending)
    latest = pending[-1] if target is None else target
    plan = compose_reply(latest["text"], entries)
    if target is None and not plan.should_send and not plan.escalate:
        # Semua pesan hanya sapaan/ucapan terima kasih -> diam tanpa eskalasi.
        # (Jangan memilih "pesan terakhir" begitu saja: urutan "berapa ya" lalu
        # "iyaa" membuat pertanyaannya hilang.)
        pass
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
                if note == "perlu_pin":
                    raise RuntimeError(
                        "Messenger meminta PIN pemulihan chat saat mengirim balasan."
                    )

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

    if dry_run and plan.should_send and not sent_text:
        # Uji kering: JANGAN tandai terjawab, supaya putaran berikutnya (tanpa uji
        # kering) masih bisa mengirim balasan ini.
        log(
            "info",
            f"[{thread.name}] belum ditandai terjawab karena uji kering "
            "(matikan uji kering untuk mengirim)",
        )
        return new_messages

    note = "dibalas" if sent_text else ("diam" if not plan.should_send else "gagal_kirim")
    db.mark_answered_many(thread.key, keys, sent_text, note=note)
    return new_messages


_QUESTION_HINTS = (
    "berapa", "harganya", "harga", "ready", "cod", "lokasi", "alamat", "stok",
    "masih ada", "bisa", "buka", "wa", "nomor", "?",
)


def _heal_unanswered(db, job_id, thread, read, counters, sender, dry_run, log) -> None:
    """Pulihkan pesan yang dulu salah ditandai 'terjawab' walau tidak pernah dibalas.

    Hanya dipakai bila Facebook sendiri menandai pesan itu **belum dibaca**: bukti
    kuat bahwa pembeli belum mendapat tanggapan. Tidak menebak dari posisi.
    """
    unread = [msg for msg in getattr(read, "messages", []) or [] if getattr(msg, "unread", False)]
    if not unread:
        return
    texts = {" ".join(msg.text.lower().split())[:60] for msg in unread}
    healed = db.reopen_messages(thread.key, texts)
    if healed:
        db.log_event(
            job_id,
            "warning",
            f"[{thread.name}] {healed} pesan dibuka kembali untuk dijawab "
            "(ditandai belum dibaca di Facebook, tetapi tercatat sudah dijawab)",
        )


def _pick_target(read, pending: list[dict]):
    """Pilih pesan lawan bicara yang layak dijawab.

    Utamakan yang ditandai **belum dibaca** Facebook, lalu yang berbunyi seperti
    permintaan. Fallback: pesan masuk terakhir yang belum ditandai terjawab.
    """
    try:
        from .browser.messenger import pick_answer_target
    except Exception:  # noqa: BLE001
        return pending[-1]
    answered = {
        " ".join(str(row.get("text") or "").lower().split())[:60]
        for row in pending
        if row.get("answered")
    }
    messages = [
        {
            "text": msg.text,
            "direction": msg.direction,
            "unread": getattr(msg, "unread", False),
        }
        for msg in getattr(read, "messages", []) or []
    ]
    picked = pick_answer_target(messages, answered_texts=answered)
    if picked is None:
        return None
    picked_text = " ".join(str(picked.get("text") or "").lower().split())
    # Petakan kembali ke baris DB (supaya teksnya persis yang tersimpan).
    for row in reversed(pending):
        if " ".join(str(row.get("text") or "").lower().split()) == picked_text:
            return row
    for row in pending:
        if picked_text and picked_text in " ".join(str(row.get("text") or "").lower().split()):
            return row
    return pending[-1]


def _looks_like_question(text: str) -> bool:
    lowered = " ".join((text or "").lower().split())
    if not lowered:
        return False
    return any(hint in lowered for hint in _QUESTION_HINTS)


def _maybe_flag_lost_question(db, job_id, thread, counters, sender, dry_run, log) -> None:
    """Chat bertanda belum dibaca/kotak berisi pertanyaan tapi tak terbaca sebagai pesan."""
    preview = getattr(thread, "preview", "") or ""
    if not (getattr(thread, "unread", False) or _looks_like_question(preview)):
        return
    reason = (
        "daftar chat menandai belum dibaca, tetapi worker tidak menemukan pesan masuk"
        if getattr(thread, "unread", False)
        else "cuplikan di daftar chat berbunyi seperti pertanyaan"
    )
    log(
        "warning",
        f"[{thread.name}] {reason}; saya beri tahu Anda lewat WhatsApp "
        f"(cuplikan: {preview[:60]!r})",
    )
    _escalate(
        db,
        job_id,
        thread,
        Escalation(
            chat_name=thread.name,
            message=preview or "(tidak ada pesan terbaca)",
            reason=f"{reason} — periksa chat ini sendiri",
            url=thread.url,
            when=datetime.now().strftime("%d/%m %H:%M"),
        ),
        counters,
        sender,
        dry_run,
        log,
    )


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
