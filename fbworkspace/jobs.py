"""Menjalankan & menghentikan worker browser dari dashboard.

Catatan: hanya proses lokal. Tidak ada layanan jaringan yang dibuka.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT, Settings
from .db import Database
from .models import JobStatus


def log_path(job_id: int, settings: Settings) -> Path:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings.log_dir / f"worker_{job_id}.log"


def start_job(
    db: Database,
    settings: Settings,
    *,
    group_urls: list[str],
    config: dict,
) -> tuple[int, subprocess.Popen]:
    """Buat job di database lalu jalankan worker sebagai proses terpisah."""
    job_id = db.create_job(kind="browser", group_urls=group_urls, config=config)
    settings.ensure_dirs()
    path = log_path(job_id, settings)

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Pastikan worker memakai settings yang sama
    env["FBW_DB_PATH"] = str(settings.db_path)
    env["FBW_BROWSER_PROFILE_DIR"] = str(settings.browser_profile_dir)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - hidup selama proses worker
    handle.write(f"\n=== worker untuk job {job_id} dimulai ===\n")
    handle.flush()
    proc = subprocess.Popen(
        [sys.executable, "-m", "fbworkspace.worker", "--job-id", str(job_id)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    db.update_job(job_id, pid=proc.pid, message="Worker dijalankan.")
    db.log_event(job_id, "info", f"Worker PID {proc.pid} dimulai.")
    return job_id, proc


def request_stop(db: Database, job_id: int) -> None:
    db.request_stop(job_id)


def force_stop(db: Database, job_id: int, proc: subprocess.Popen | None) -> str:
    """Hentikan paksa. Dipakai hanya jika 'minta berhenti' tidak bekerja."""
    db.request_stop(job_id)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            db.update_job(job_id, status=JobStatus.STOPPED.value, message="Dihentikan paksa.", finished_at=_now())
            db.log_event(job_id, "warning", "Worker dihentikan paksa oleh pengguna.")
            return "Proses worker dihentikan paksa."
        except Exception as exc:  # noqa: BLE001
            return f"Gagal menghentikan proses: {exc}"
    db.update_job(
        job_id,
        status=JobStatus.STOPPED.value,
        message="Ditandai berhenti (proses sudah tidak berjalan).",
        finished_at=_now(),
    )
    db.log_event(job_id, "warning", "Job ditandai berhenti; proses worker tidak ditemukan.")
    return "Job ditandai berhenti."


def mark_failed(db: Database, job_id: int, reason: str) -> None:
    db.update_job(
        job_id,
        status=JobStatus.FAILED.value,
        message=reason,
        finished_at=_now(),
    )
    db.log_event(job_id, "error", reason)


def tail_log(job_id: int, settings: Settings, max_lines: int = 80) -> str:
    path = log_path(job_id, settings)
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def worker_config(
    *,
    mode: str,
    max_posts: int,
    max_seconds: int,
    filter_json: dict,
    strict_unknown: bool,
    analyze_ollama: bool,
) -> dict:
    return {
        "mode": mode,
        "max_posts": int(max_posts),
        "max_seconds": int(max_seconds),
        "filter": filter_json,
        "strict_unknown": bool(strict_unknown),
        "analyze_ollama": bool(analyze_ollama),
    }


def config_preview(config: dict) -> str:
    """Ringkasan konfigurasi job untuk ditampilkan (tanpa data sensitif)."""
    safe = {
        "mode": config.get("mode"),
        "max_posts": config.get("max_posts"),
        "max_seconds": config.get("max_seconds"),
        "filter": config.get("filter"),
        "strict_unknown": config.get("strict_unknown"),
        "analyze_ollama": config.get("analyze_ollama"),
    }
    return json.dumps(safe, ensure_ascii=False, indent=2)


def _now() -> str:
    from .models import utcnow_iso

    return utcnow_iso()
