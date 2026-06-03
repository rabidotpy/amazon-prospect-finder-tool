"""Green-prospect rule + CSV export.

A GREEN prospect is the kind we want to pitch: a brand with proven Amazon
revenue that is NOT yet investing in a direct-to-consumer presence, so there is
clear room to sell them services. Concretely:

    GREEN  ==  has NO website
           AND running fewer than GREEN_MAX_ADS Meta ads
           AND running fewer than GREEN_MAX_ADS Google ads

Unknown ad counts (None = we couldn't scrape) are treated as 0 here: a brand
with no website almost never runs ads, and a blank means "no ads detected", not
"many ads". The rep verifies before pitching — the CSV gives them everything
needed to make that call.
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


def is_green(has_website, meta_count, google_count, max_ads=None):
    """The single source of truth for the green rule. Returns 1 or 0."""
    max_ads = config.GREEN_MAX_ADS if max_ads is None else max_ads
    if has_website:
        return 0
    if (meta_count or 0) >= max_ads:
        return 0
    if (google_count or 0) >= max_ads:
        return 0
    return 1


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
        n += g
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
