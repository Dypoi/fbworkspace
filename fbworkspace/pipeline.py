"""Alur pemrosesan: normalisasi -> dedupe -> simpan -> filter -> analisis aturan.

Dipakai oleh worker browser maupun input manual, supaya perilakunya konsisten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analyzer import analyze_text
from .db import Database
from .filters import evaluate
from .models import UNKNOWN, FilterConfig, Source
from .normalize import normalize_text, parse_posted_at


@dataclass
class IngestOutcome:
    post_id: int
    is_new: bool
    filter_passed: bool
    filter_reasons: list[str] = field(default_factory=list)
    category: str = UNKNOWN
    relevance_score: int = 0
    error: str = ""


@dataclass
class ManualEntry:
    text: str
    post_url: str | None = None
    group_url: str = ""
    posted_at_raw: str | None = None
    note: str = ""


def ingest_post(
    db: Database,
    *,
    text: str,
    source: str = Source.MANUAL.value,
    group_url: str = "",
    post_url: str | None = None,
    posted_at_raw: str | None = None,
    filter_config: FilterConfig | None = None,
    strict_unknown: bool = False,
    save_rule_analysis: bool = True,
) -> IngestOutcome:
    """Simpan satu postingan + hasil filter/analisis aturan.

    Mengembalikan IngestOutcome; tidak melempar exception untuk teks kosong,
    tetapi mengembalikan error yang jelas.
    """
    clean = normalize_text(text)
    if not clean:
        return IngestOutcome(0, False, False, [], error="Teks kosong setelah normalisasi.")

    cfg = filter_config or FilterConfig()
    analysis = analyze_text(clean)
    filter_result = evaluate(
        clean, cfg, analysis=analysis.as_dict(), strict_unknown=strict_unknown
    )

    posted_at, precision, _note = parse_posted_at(posted_at_raw)

    try:
        post_id, is_new = db.insert_post(
            text=clean,
            group_url=group_url,
            post_url=post_url,
            source=source,
            posted_at_raw=posted_at_raw,
            posted_at=posted_at,
            posted_at_precision=precision,
            filter_passed=filter_result.passed,
            filter_reasons=filter_result.reasons,
        )
    except ValueError as exc:
        return IngestOutcome(0, False, False, [], error=str(exc))

    if is_new and save_rule_analysis:
        db.save_analysis(post_id, analysis.as_dict())

    return IngestOutcome(
        post_id=post_id,
        is_new=is_new,
        filter_passed=filter_result.passed,
        filter_reasons=filter_result.reasons,
        category=analysis.category,
        relevance_score=analysis.relevance_score,
    )


_BLOCK_SPLIT = re.compile(r"^\s*-{3,}\s*$", re.M)


def parse_manual_input(raw: str) -> list[ManualEntry]:
    """Pecah input manual menjadi beberapa postingan.

    Format yang didukung (opsional):
        URL: https://www.facebook.com/groups/123/posts/456
        GRUP: https://www.facebook.com/groups/123/
        WAKTU: 3 jam lalu
        <teks postingan>

        ---

        <postingan berikutnya>

    Baris tanpa awalan dianggap bagian dari teks postingan.
    """
    entries: list[ManualEntry] = []
    blocks = _BLOCK_SPLIT.split(raw or "")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        post_url = None
        group_url = ""
        posted_at_raw = None
        body_lines: list[str] = []
        for line in block.splitlines():
            match = re.match(r"^\s*(URL|POST|TAUTAN)\s*:\s*(\S+)\s*$", line, re.I)
            group_match = re.match(r"^\s*(GRUP|GROUP)\s*:\s*(\S+)\s*$", line, re.I)
            time_match = re.match(r"^\s*(WAKTU|TANGGAL)\s*:\s*(.+)$", line, re.I)
            if match:
                post_url = match.group(2)
            elif group_match:
                group_url = group_match.group(2)
            elif time_match:
                posted_at_raw = time_match.group(2).strip()
            else:
                body_lines.append(line)
        text = normalize_text("\n".join(body_lines))
        if text:
            entries.append(
                ManualEntry(
                    text=text,
                    post_url=post_url,
                    group_url=group_url,
                    posted_at_raw=posted_at_raw,
                )
            )
    return entries


@dataclass
class BatchSummary:
    read: int = 0
    inserted: int = 0
    duplicates: int = 0
    filtered_out: int = 0
    errors: int = 0
    messages: list[str] = field(default_factory=list)

    def counters(self) -> dict[str, int]:
        return {
            "read": self.read,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "filtered_out": self.filtered_out,
            "errors": self.errors,
        }


def ingest_manual_entries(
    db: Database,
    entries: list[ManualEntry],
    *,
    filter_config: FilterConfig,
    strict_unknown: bool = False,
    max_posts: int = 50,
) -> BatchSummary:
    summary = BatchSummary()
    for entry in entries[: max(1, max_posts)]:
        summary.read += 1
        outcome = ingest_post(
            db,
            text=entry.text,
            source=Source.MANUAL.value,
            group_url=entry.group_url,
            post_url=entry.post_url,
            posted_at_raw=entry.posted_at_raw,
            filter_config=filter_config,
            strict_unknown=strict_unknown,
        )
        if outcome.error:
            summary.errors += 1
            summary.messages.append(f"Baris {summary.read}: {outcome.error}")
            continue
        if not outcome.is_new:
            summary.duplicates += 1
            continue
        summary.inserted += 1
        if not outcome.filter_passed:
            summary.filtered_out += 1
    return summary
