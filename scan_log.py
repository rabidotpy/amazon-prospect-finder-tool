"""Structured scan-event log — the feed behind the live Scan Logs page.

Every stage of a scan appends one JSON line to data/scan_events.log:
  • brand_start  — a brand enters the pipeline (which signals it will scan)
  • phase        — a signal (website/meta/google) finished, with its duration+result
  • ai_call      — a claude call: purpose, model, duration, prompt preview, result, cost
  • brand_done   — a brand finished, with its total time
  • note         — any other important message

Because each brand is scanned in its OWN subprocess, they all append to the same
shared file (append mode, short lines) and the dashboard reads it. The file is
size-capped so it can't grow without bound.
"""

import datetime as dt
import json
import os

import db

LOG_PATH = os.path.join(os.path.dirname(db.DB_PATH), "scan_events.log")
_MAX_BYTES = 4_000_000
_KEEP_LINES = 1500


def emit(kind, **fields):
    """Append one structured event. Never raises into the caller."""
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind}
    rec.update(fields)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(LOG_PATH) > _MAX_BYTES:
            _truncate()
    except Exception:
        pass


def _truncate():
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-_KEEP_LINES:])
        os.replace(tmp, LOG_PATH)
    except Exception:
        pass


def tail(n=250):
    """Most recent `n` events as dicts (oldest first)."""
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def clear():
    try:
        open(LOG_PATH, "w").close()
    except Exception:
        pass
