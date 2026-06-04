"""Green qualification — S2-1: 'unknown is never zero', three-valued state."""
import config
import green
from conftest import insert_brand

MAX = config.GREEN_MAX_ADS


# --------------------------------------------------------- pure rule (green_state)
import pytest


@pytest.mark.parametrize("site,meta,goog,expected", [
    # verified greens: no site, both counts KNOWN and below the cap
    (0, 0, 0, "green"),
    (0, MAX - 1, MAX - 1, "green"),
    (0, 5, 5, "green"),
    # disqualified: has a website (regardless of ads)
    (1, 0, 0, "no"),
    (1, None, None, "no"),
    # disqualified: a KNOWN count at/over the cap (even if the other is unknown)
    (0, MAX, 0, "no"),
    (0, 0, MAX, "no"),
    (0, MAX, None, "no"),
    (0, None, MAX + 5, "no"),
    # candidate: no site but ad presence UNVERIFIED (a count is None)
    (0, None, None, "candidate"),
    (0, None, 0, "candidate"),
    (0, 0, None, "candidate"),
    (0, MAX - 1, None, "candidate"),
])
def test_green_state(site, meta, goog, expected):
    assert green.green_state(site, meta, goog) == expected


def test_is_green_null_safe():
    """THE regression (S2-1): no website + unknown ad counts must NOT be green=1."""
    assert green.is_green(0, None, None) is None
    assert green.is_green(0, None, None) != 1
    assert green.is_green(0, None, 0) is None
    assert green.is_green(0, 0, None) is None


def test_is_green_flag_values():
    assert green.is_green(0, 0, 0) == 1            # verified green
    assert green.is_green(1, 0, 0) == 0            # has site
    assert green.is_green(0, MAX, 0) == 0          # known heavy advertiser
    assert green.is_green(0, None, None) is None   # candidate


def test_cap_boundary():
    assert green.green_state(0, MAX - 1, MAX - 1) == "green"
    assert green.green_state(0, MAX, MAX - 1) == "no"
    assert green.green_state(0, MAX - 1, MAX) == "no"


# --------------------------------------------------------- DB integration
def test_recompute_assigns_three_states(conn):
    insert_brand(conn, "verified", has_website=0, meta=0, google=0)
    insert_brand(conn, "candidate", has_website=0, meta=None, google=None)
    insert_brand(conn, "has_site", has_website=1, meta=0, google=0)
    insert_brand(conn, "heavy_ads", has_website=0, meta=MAX, google=0)

    n = green.recompute_all(conn)

    flags = {r["brand_key"]: r["is_green"]
             for r in conn.execute("SELECT brand_key, is_green FROM brands")}
    assert flags["verified"] == 1
    assert flags["candidate"] is None
    assert flags["has_site"] == 0
    assert flags["heavy_ads"] == 0
    assert n == 1          # recompute_all counts verified greens only


def test_mark_one_matches_recompute(conn):
    insert_brand(conn, "cand", has_website=0, meta=None, google=None)
    assert green.mark_one(conn, "cand") is None
    row = conn.execute("SELECT is_green FROM brands WHERE brand_key='cand'").fetchone()
    assert row["is_green"] is None


def test_export_csv_excludes_candidates(conn, tmp_path):
    insert_brand(conn, "verified", has_website=0, meta=0, google=0)
    insert_brand(conn, "candidate", has_website=0, meta=None, google=None)
    green.recompute_all(conn)
    path = str(tmp_path / "green.csv")
    count = green.export_csv(conn, path)
    text = open(path).read()
    assert count == 1                       # only the verified green
    assert "Verified" in text               # verified brand present
    assert "candidate" not in text.lower()  # candidate excluded from the pitch CSV
