"""Parse the gamdl cron log and import runs into the UI database.

The cron runs executed before the UI existed aren't visible in its run history.
This module scans `/gamdl-logs/run.log` (written by `gamdl/run.sh`) and inserts
one runs-row per START/OK/FAIL sequence it finds.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import store

GAMDL_LOG = Path(os.environ.get("GAMDL_LOGS_DIR", "/gamdl-logs")) / "run.log"

LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(?P<tz>\S+)\]\s+"
    r"(?P<tag>START|OK|FAIL)\s+(?P<kind>artist|playlist):\s+(?P<url>\S+)"
)


@dataclass
class ParsedEvent:
    ts: float
    tag: str
    kind: str
    url: str


def _parse_ts(date_str: str, tz_str: str) -> float:
    # tz_str is "BST" or "GMT"; both are UK time. Treat as Europe/London epoch
    # by converting via the host's time-parsing with time.strptime + mktime
    # relative to UTC offset. Good enough — the UI shows local time anyway.
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    return time.mktime(dt.timetuple())  # interpreted as local time (container TZ=Europe/London)


def _iter_events(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(errors="replace").splitlines():
        m = LINE_RE.match(raw)
        if not m:
            continue
        yield ParsedEvent(
            ts=_parse_ts(m.group("ts"), m.group("tz")),
            tag=m.group("tag"),
            kind=m.group("kind"),
            url=m.group("url"),
        )


def run() -> dict:
    """Insert missing runs. Idempotent: keyed on (url, started_at rounded to int)."""
    if not GAMDL_LOG.exists():
        return {"inserted": 0, "skipped": 0, "log_present": False}
    inserted = 0
    skipped = 0
    current: dict[tuple[str, str], ParsedEvent] = {}
    events = list(_iter_events(GAMDL_LOG))

    # Seen set of (url, int(started_at)) already in DB
    with store.connect() as conn:
        rows = conn.execute("SELECT url, CAST(started_at AS INTEGER) AS s FROM runs").fetchall()
        seen = {(r["url"], r["s"]) for r in rows}

    for ev in events:
        key = (ev.url, ev.kind)
        if ev.tag == "START":
            current[key] = ev
        elif ev.tag in ("OK", "FAIL"):
            start = current.pop(key, None)
            if start is None:
                continue  # log truncated or missing START
            started_at = start.ts
            finished_at = ev.ts
            row_key = (ev.url, int(started_at))
            if row_key in seen:
                skipped += 1
                continue
            exit_code = 0 if ev.tag == "OK" else 1
            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO runs (url, kind, started_at, finished_at, exit_code, status, trigger, log_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ev.url,
                        ev.kind,
                        started_at,
                        finished_at,
                        exit_code,
                        "ok" if exit_code == 0 else "failed",
                        "cron",
                        str(GAMDL_LOG),
                    ),
                )
            inserted += 1
            seen.add(row_key)
    return {"inserted": inserted, "skipped": skipped, "log_present": True}
