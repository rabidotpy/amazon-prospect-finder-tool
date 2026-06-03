"""Local config + secrets loader (never hardcode tokens in source).

Reads from, in order:
  1. environment variables
  2. secrets.json next to this file (git-ignored)

FB_ADLIB_TOKEN — a Facebook Ad Library API token. NOTE these tokens are
short-lived (they expire in ~1-2 hours) and the public ads_archive endpoint
only returns political/issue/electoral ads OUTSIDE the EU, so it cannot count a
US commercial brand's ads. We therefore use the web Ad Library page scrape for
counts (see adscrape.py) and treat the API as an optional EU/political extra.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SECRETS = os.path.join(_HERE, "secrets.json")


def _load_secrets():
    try:
        with open(_SECRETS) as fh:
            return json.load(fh)
    except Exception:
        return {}


_SECRETS_CACHE = _load_secrets()


def get(name, default=None):
    return os.environ.get(name) or _SECRETS_CACHE.get(name) or default


FB_ADLIB_TOKEN = get("FB_ADLIB_TOKEN", "")


def _int(name, default):
    try:
        return int(get(name, default))
    except Exception:
        return default


def _float(name, default):
    try:
        return float(get(name, default))
    except Exception:
        return default


# --- automation / green-rule tunables -----------------------------------
# A brand is "green" (worth pitching) if it has no website AND runs fewer than
# this many ads on EACH of Meta and Google.
GREEN_MAX_ADS = _int("GREEN_MAX_ADS", 30)
# Daemon: seconds between Downloads polls when idle (no pending brands).
POLL_SECONDS = _int("POLL_SECONDS", 60)
# Daemon: polite pause between enriching one brand and the next.
ENRICH_DELAY = _float("ENRICH_DELAY", 1.5)
