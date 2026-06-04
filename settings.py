"""User settings + the daily scan budget.

Two small JSON files under data/ (separate, to avoid the dashboard and the worker
writing the same file):

  • settings.json      — user preferences (daily_scan_cap). Edited from the UI.
  • scan_counter.json  — runtime counter of DISTINCT brands scanned *today*.

The cap is read LIVE on every check, so changing it (in the UI or by editing the
file) takes effect immediately — a running job that is over the new cap stops
launching new brands at once. "Scanned today" counts distinct brand_keys that did
real work today and resets at local midnight.
"""

import datetime as dt
import json
import os

import db

_DIR = os.path.dirname(db.DB_PATH)
SETTINGS_PATH = os.path.join(_DIR, "settings.json")
COUNTER_PATH = os.path.join(_DIR, "scan_counter.json")

DEFAULT_DAILY_CAP = 100


# ------------------------------------------------------------------ settings
def _read(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def get_daily_cap():
    """The live daily scan cap (re-read every call). Default 100."""
    try:
        return max(0, int(_read(SETTINGS_PATH).get("daily_scan_cap", DEFAULT_DAILY_CAP)))
    except Exception:
        return DEFAULT_DAILY_CAP


def set_daily_cap(n):
    s = _read(SETTINGS_PATH)
    s["daily_scan_cap"] = max(0, int(n))
    _write(SETTINGS_PATH, s)
    return s["daily_scan_cap"]


# --------------------------------------------------------------- daily counter
def _today():
    return dt.date.today().isoformat()


def _counter():
    d = _read(COUNTER_PATH)
    if d.get("date") != _today():       # new day -> reset
        d = {"date": _today(), "keys": []}
    return d


def scanned_today():
    return len(_counter().get("keys", []))


def record_scan(brand_key):
    """Mark a brand as scanned today (distinct). Returns the new daily count."""
    d = _counter()
    if brand_key and brand_key not in d["keys"]:
        d["keys"].append(brand_key)
        _write(COUNTER_PATH, d)
    return len(d["keys"])


def remaining_today():
    return max(0, get_daily_cap() - scanned_today())


def can_scan():
    """True while today's scan count is below the live cap."""
    return remaining_today() > 0
