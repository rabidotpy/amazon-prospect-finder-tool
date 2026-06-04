"""Background-worker heartbeat + status — the single source of truth the dashboard
reads to tell the user, at a glance:

  • is the worker running, idle, or stopped (and how long since we last heard from it)?
  • which job + which brand is it processing right now (done/total)?
  • did it die mid-job, leaving interrupted job data to clean up?

The worker (`auto.py` / `ad_jobs.run_job`) writes a small JSON heartbeat frequently;
the dashboard reads it. A heartbeat older than STALE_SECONDS means the worker is not
actually alive (crashed/killed) even if a zombie process lingers.
"""

import datetime as dt
import json
import os

import db

STATUS_PATH = os.path.join(os.path.dirname(db.DB_PATH), "worker_status.json")
STALE_SECONDS = 90      # no heartbeat in this long => worker is not alive


def _now():
    return dt.datetime.now()


def write(state, **fields):
    """Persist a heartbeat. `state` ∈ {starting, idle, scanning, stopped}.
    Preserves `started_at` across writes; stamps `heartbeat`=now."""
    prev = read() or {}
    data = {
        "state": state,
        "pid": os.getpid(),
        "heartbeat": _now().isoformat(timespec="seconds"),
        "started_at": prev.get("started_at") or _now().isoformat(timespec="seconds"),
    }
    data.update(fields)
    if state in ("idle", "stopped"):          # clear stale activity fields
        data.setdefault("current", None)
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, STATUS_PATH)
    return data


def read():
    try:
        with open(STATUS_PATH) as fh:
            return json.load(fh)
    except Exception:
        return None


def age_seconds(data=None):
    """Seconds since the last heartbeat, or None if unknown."""
    data = read() if data is None else data
    if not data or not data.get("heartbeat"):
        return None
    try:
        hb = dt.datetime.fromisoformat(data["heartbeat"])
    except Exception:
        return None
    return (_now() - hb).total_seconds()


def is_alive(data=None):
    """True only if we've heard from the worker recently and it isn't stopped."""
    data = read() if data is None else data
    if not data or data.get("state") == "stopped":
        return False
    age = age_seconds(data)
    return age is not None and age < STALE_SECONDS


def summary():
    """Human-facing status dict for the dashboard."""
    data = read() or {}
    age = age_seconds(data)
    alive = is_alive(data)
    state = data.get("state", "unknown")
    if not alive and state not in ("stopped", "unknown"):
        state = "stopped"          # heartbeat went stale => treat as stopped
    return {
        "alive": alive,
        "state": state,
        "current": data.get("current"),
        "job_id": data.get("job_id"),
        "done": data.get("done"),
        "total": data.get("total"),
        "heartbeat": data.get("heartbeat"),
        "age": age,
        "started_at": data.get("started_at"),
        "last_error": data.get("last_error"),
    }
