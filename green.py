"""Green-prospect rule + CSV export.

A GREEN prospect is the kind we want to pitch: a brand with proven Amazon
revenue that is NOT yet investing in a direct-to-consumer presence, so there is
clear room to sell them services. Concretely:

    GREEN  ==  has NO website
           AND running fewer than GREEN_MAX_ADS Meta ads   (count KNOWN)
           AND running fewer than GREEN_MAX_ADS Google ads (count KNOWN)

Three-valued, on purpose. "Unknown is never zero": a NULL ad count means we have
not verified the brand's ad presence — it is NOT evidence of zero ads. So a brand
with no website but an unverified ad count is a **candidate**, not a confirmed
green. We never claim green without knowing the ads.

    green_state() -> "green"     (verified: no site, both counts known & low)
                     "candidate" (no site, but ad presence UNVERIFIED)
                     "no"        (has a site, or a KNOWN count at/over the cap)

The `brands.is_green` column stores the flag form: 1 / 0 / NULL respectively.
Only verified greens (is_green=1) are exported to the pitch CSV.
"""

import csv
import io
import os

import config
import db

GREEN_CSV = os.path.join(os.path.dirname(db.DB_PATH), "green_prospects.csv")

# Columns written to the CSV / shown on the Green page, in order.
CSV_COLS = [
    "brand", "parent_level_revenue", "parent_level_sales", "total_reviews",
    "asin_count", "seller_count", "min_listing_age", "prospect_signal",
    "has_website", "meta_ads_count", "google_ads_count",
    "website_remarks", "example_url", "meta_ads_url", "google_ads_url",
    "facebook", "instagram", "tiktok", "youtube", "enriched_at",
]


def green_state(has_website, meta_count, google_count, max_ads=None):
    """Three-valued green classification — the single source of truth.

    Returns "green" | "candidate" | "no". A KNOWN count at/over the cap
    disqualifies even if the other count is unknown; otherwise BOTH counts must be
    known (and low) to confirm green — an unknown count is never treated as zero.
    """
    max_ads = config.GREEN_MAX_ADS if max_ads is None else max_ads
    if has_website:
        return "no"
    if meta_count is not None and meta_count >= max_ads:
        return "no"
    if google_count is not None and google_count >= max_ads:
        return "no"
    if meta_count is None or google_count is None:
        return "candidate"          # no site, but ad presence unverified
    return "green"


_STATE_FLAG = {"green": 1, "no": 0, "candidate": None}


def is_green(has_website, meta_count, google_count, max_ads=None):
    """Flag form of `green_state` for the `brands.is_green` column:
    1 = verified green, 0 = not green, None = candidate (ads unverified)."""
    return _STATE_FLAG[green_state(has_website, meta_count, google_count, max_ads)]


def mark_one(conn, brand_key):
    """Recompute is_green for a single brand from its current row."""
    r = conn.execute(
        "SELECT has_website, meta_ads_count, google_ads_count FROM brands "
        "WHERE brand_key=?", (brand_key,)).fetchone()
    if not r:
        return 0
    g = is_green(r["has_website"], r["meta_ads_count"], r["google_ads_count"])
    conn.execute("UPDATE brands SET is_green=? WHERE brand_key=?", (g, brand_key))
    conn.commit()
    return g


def recompute_all(conn):
    """Recompute is_green across every brand (e.g. after a threshold change)."""
    rows = conn.execute(
        "SELECT brand_key, has_website, meta_ads_count, google_ads_count "
        "FROM brands").fetchall()
    n = 0
    for r in rows:
        g = is_green(r["has_website"], r["meta_ads_count"], r["google_ads_count"])
        conn.execute("UPDATE brands SET is_green=? WHERE brand_key=?",
                     (g, r["brand_key"]))
        if g == 1:                      # count verified greens only
            n += 1
    conn.commit()
    return n


def export_csv(conn, path=GREEN_CSV):
    """Write all green brands (revenue-sorted) to `path`. Returns the row count."""
    rows = conn.execute(
        "SELECT * FROM brands WHERE is_green=1 AND enriched_at IS NOT NULL "
        "ORDER BY parent_level_revenue DESC").fetchall()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        d = {k: (r[k] if k in r.keys() else "") for k in CSV_COLS}
        w.writerow(d)
    # atomic-ish write
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)
    return len(rows)
