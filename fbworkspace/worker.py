"""Worker pembacaan browser yang berjalan sebagai proses terpisah.

Kenapa proses terpisah?
- Streamlit tidak perlu "menahan" Playwright di dalam siklus rerun.
- Tombol Berhenti bisa diproses lewat database (stop_requested) tanpa mematikan UI.
- Kalau browser error, dashboard tetap hidup dan status kegagalan tetap tercatat.

Cara pakai (biasanya dipanggil dashboard):
    python -m fbworkspace.worker --job-id 12
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from .browser.base import CollectTarget
from .browser.manual import ManualAdapter
from .browser.playwright_fb import PlaywrightFacebookAdapter
from .config import get_settings
from .db import Database
from .filters import config_from_json
from .models import AccessStatus, CollectLimits, JobStatus, Source

_LOG_LIMIT = 400


def _adapter_for(mode: str, settings):
    if mode == "manual":
        return ManualAdapter()
    return PlaywrightFacebookAdapter(
        profile_dir=settings.browser_profile_dir,
        headless=settings.browser_headless,
        slow_mo_ms=settings.browser_slow_mo_ms,
        # Saat login wall muncul, browser dibiarkan terbuka agar pengguna bisa
        # login sendiri (lihat FBW_LOGIN_WAIT_SECONDS).
        login_wait_seconds=settings.login_wait_seconds,
    )


def run_job(job_id: int) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)

    job = db.get_job(job_id)
    if job is None:
        print(f"Job {job_id} tidak ditemukan.", file=sys.stderr)
        return 2

    config = job.get("config") or {}
    group_urls = [u for u in (job.get("group_urls") or []) if u]
    filter_config = config_from_json(config.get("filter") or {})
    strict_unknown = bool(config.get("strict_unknown"))
    mode = config.get("mode", "playwright")

    limits = CollectLimits(
        max_posts=int(config.get("max_posts") or settings.default_max_posts),
        max_seconds=int(config.get("max_seconds") or settings.default_max_minutes * 60),
    ).clamp(settings.hard_max_posts)

    counters = {
        "read": 0, "inserted": 0, "duplicates": 0, "filtered_out": 0,
        "analyzed": 0, "ollama_analyzed": 0, "errors": 0,
        "skipped_comment": 0,
    }
    new_relevant_ids: list[int] = []
    last_logged: list[str] = [""]

    db.update_job(
        job_id,
        status=JobStatus.RUNNING.value,
        started_at=job.get("started_at") or _now(),
        heartbeat_at=_now(),
        pid=os.getpid(),
        message="Worker dimulai.",
    )
    db.log_event(job_id, "info", f"Worker mulai. Mode={mode}, batas={limits.max_posts} postingan / {limits.max_seconds} detik.")

    def push_counters() -> None:
        import json

        db.update_job(
            job_id,
            counters_json=json.dumps(counters),
            heartbeat_at=_now(),
        )

    def on_progress(level: str, message: str, counts: dict | None = None) -> None:
        if counts:
            counters["read"] = max(counters["read"], int(counts.get("read", counters["read"])))
            counters["skipped_comment"] = max(
                counters["skipped_comment"],
                int(counts.get("skipped_comment", counters["skipped_comment"])),
            )
        push_counters()
        if message != last_logged[0]:
            db.log_event(job_id, level, str(message)[:500])
            last_logged[0] = message
        db.update_job(job_id, message=str(message)[:400])

    status_value = JobStatus.COMPLETED.value
    message = ""
    try:
        adapter = _adapter_for(mode, settings)
        available, why = adapter.is_available()
        if not available:
            db.log_event(job_id, "error", why)
            db.update_job(
                job_id,
                status=JobStatus.FAILED.value,
                message=why,
                finished_at=_now(),
                counters_json=_counters_json(counters),
            )
            return 1

        target = CollectTarget(group_urls=group_urls, limits=limits)
        outcome = adapter.collect(
            target,
            should_stop=lambda: db.is_stop_requested(job_id),
            on_progress=on_progress,
        )

        skipped_comments = int(outcome.diagnostics.get("skipped_comment") or 0)
        counters["skipped_comment"] = max(counters["skipped_comment"], skipped_comments)
        db.log_event(job_id, "info", f"Adapter selesai dengan status: {outcome.status.value}")
        if skipped_comments:
            db.log_event(
                job_id,
                "warning",
                f"{skipped_comments} elemen mirip komentar/reply dilewati (bukan isi postingan).",
            )
        for note in outcome.diagnostics.get("notes", [])[:5]:
            db.log_event(job_id, "warning", note)

        # --- Simpan postingan yang berhasil dibaca (termasuk hasil sebagian) ---
        from .pipeline import ingest_post

        counters["read"] = len(outcome.posts)
        for raw in outcome.posts:
            if db.is_stop_requested(job_id):
                outcome.status = AccessStatus.STOPPED
                break
            result = ingest_post(
                db,
                text=raw.text,
                source=Source.BROWSER.value,
                group_url=raw.group_url,
                post_url=raw.post_url,
                posted_at_raw=raw.posted_at_raw,
                filter_config=filter_config,
                strict_unknown=strict_unknown,
            )
            if result.error:
                counters["errors"] += 1
                db.log_event(job_id, "error", f"Gagal menyimpan satu postingan: {result.error}")
                continue
            if not result.is_new:
                counters["duplicates"] += 1
                continue
            counters["inserted"] += 1
            counters["analyzed"] += 1
            if not result.filter_passed:
                counters["filtered_out"] += 1
            else:
                new_relevant_ids.append(result.post_id)
        push_counters()
        db.log_event(
            job_id, "info",
            f"Penyimpanan selesai: {counters['inserted']} baru, {counters['duplicates']} duplikat, "
            f"{counters['filtered_out']} tidak lolos filter, "
            f"{counters['skipped_comment']} komentar dilewati."
        )

        # --- Analisis Ollama opsional ---
        if config.get("analyze_ollama") and new_relevant_ids:
            status_value, message = _run_ollama(
                db, job_id, settings, config, new_relevant_ids, counters, push_counters
            )
        else:
            status_value, message = _map_outcome_status(outcome, counters)

    except KeyboardInterrupt:
        status_value = JobStatus.STOPPED.value
        message = "Worker dihentikan (Ctrl+C)."
        db.log_event(job_id, "warning", message)
    except Exception as exc:  # noqa: BLE001 - worker tidak boleh mati tanpa status
        status_value = JobStatus.FAILED.value
        message = f"Worker gagal: {exc.__class__.__name__}: {exc}"
        db.log_event(job_id, "error", message)
        db.log_event(job_id, "error", traceback.format_exc()[:1500])
    finally:
        if db.is_stop_requested(job_id) and status_value == JobStatus.COMPLETED.value:
            status_value = JobStatus.STOPPED.value
            message = message or "Dihentikan oleh pengguna."
        db.update_job(
            job_id,
            status=status_value,
            message=message,
            finished_at=_now(),
            heartbeat_at=_now(),
            counters_json=_counters_json(counters),
        )
        db.log_event(job_id, "info", f"Job selesai: {status_value}. {message}")
        print(f"[worker] job {job_id}: {status_value} - {message}")
    return 0


def _run_ollama(db, job_id, settings, config, post_ids, counters, push_counters):
    """Jalankan Ollama untuk postingan yang lolos filter. Gagal AI tidak menggagalkan job."""
    from .ollama_client import OllamaConfig, analyze_post, check_connection

    status = check_connection(OllamaConfig.from_settings(settings))
    if not status.ok:
        db.log_event(job_id, "warning", f"Ollama tidak dipakai: {status.message}")
        return JobStatus.COMPLETED.value, (
            "Pembacaan selesai. Analisis Ollama dilewati karena server/model tidak tersedia."
        )

    ollama_config = OllamaConfig.from_settings(settings)
    failures = 0
    for post_id in post_ids[:50]:
        if db.is_stop_requested(job_id):
            return JobStatus.STOPPED.value, "Dihentikan saat analisis Ollama."
        post = db.get_post(post_id)
        if not post:
            continue
        db.update_job(job_id, message=f"Analisis Ollama untuk postingan #{post_id}…")
        result = analyze_post(post["text"], ollama_config)
        if result.ok and result.analysis:
            db.save_analysis(post_id, result.analysis, raw_output=result.raw)
            counters["ollama_analyzed"] += 1
        else:
            failures += 1
            db.set_analysis_status(post_id, "analisis gagal")
            db.log_event(job_id, "warning", f"Ollama gagal untuk #{post_id}: {result.error}")
        push_counters()
    if failures and counters["ollama_analyzed"] == 0:
        return JobStatus.COMPLETED.value, (
            "Pembacaan selesai. Analisis Ollama gagal untuk semua postingan; "
            "hasil filter kata kunci tetap tersimpan."
        )
    return JobStatus.COMPLETED.value, (
        f"Selesai. {counters['inserted']} postingan baru, {counters['ollama_analyzed']} dianalisis Ollama."
    )


def _map_outcome_status(outcome, counters) -> tuple[str, str]:
    message = outcome.message
    skipped = int(counters.get("skipped_comment") or 0)
    if skipped and str(skipped) not in message:
        message = f"{message} ({skipped} komentar/reply dilewati.)"
    if outcome.status in {AccessStatus.OK, AccessStatus.OK_EMPTY}:
        return JobStatus.COMPLETED.value, message
    if outcome.status == AccessStatus.STOPPED:
        return JobStatus.STOPPED.value, message
    if outcome.status == AccessStatus.ERROR:
        return JobStatus.FAILED.value, message
    return JobStatus.BLOCKED.value, message


def _counters_json(counters: dict) -> str:
    import json

    return json.dumps(counters)


def _now() -> str:
    from .models import utcnow_iso

    return utcnow_iso()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FB Workspace - worker pembacaan browser")
    parser.add_argument("--job-id", type=int, required=True, help="ID job di database")
    args = parser.parse_args(argv)
    return run_job(args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
