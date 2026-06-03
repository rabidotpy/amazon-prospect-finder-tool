"""SQLite schema + helpers for the Amazon prospecting app.

Three tables:
  products  - one row per ASIN per import (the raw Helium10 grain)
  brands    - one row per unique brand (the lead grain)
  sellers   - one row per unique seller

parent_level_revenue is unique per (seller, brand) parent and repeats across a
parent's child ASINs, so aggregation groups by distinct value to avoid double
counting (see aggregate_*).
"""

import os
import re
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prospects.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT, title TEXT,
    brand TEXT, brand_key TEXT,
    seller TEXT, seller_key TEXT,
    url TEXT, image_url TEXT,
    category TEXT, subcategory TEXT,
    price REAL, bsr INTEGER, subcategory_bsr INTEGER,
    asin_sales INTEGER, asin_revenue REAL,
    parent_level_sales INTEGER, parent_level_revenue REAL,
    review_count INTEGER, reviews_rating REAL,
    listing_age_months REAL, fulfillment TEXT,
    active_sellers INTEGER, seller_country TEXT,
    variation_count INTEGER, number_of_images INTEGER,
    source_file TEXT, imported_at TEXT,
    UNIQUE(asin)
);

CREATE TABLE IF NOT EXISTS brands (
    brand_key TEXT PRIMARY KEY,
    brand TEXT,
    website_url TEXT, website_urls TEXT, website_remarks TEXT, has_website INTEGER,
    meta_ads_url TEXT, google_ads_url TEXT,
    meta_ads_count INTEGER, google_ads_count INTEGER,
    is_meta_ads INTEGER, is_google_ads INTEGER,
    meta_link_kind TEXT, fb_page_id TEXT,
    facebook TEXT, instagram TEXT, tiktok TEXT, youtube TEXT,
    parent_level_revenue REAL, parent_level_sales INTEGER,
    total_reviews INTEGER, asin_count INTEGER, seller_count INTEGER,
    min_listing_age REAL, prospect_signal TEXT, confidence TEXT,
    example_url TEXT, example_asin TEXT, example_title TEXT,
    is_green INTEGER,
    enriched_at TEXT
);

CREATE TABLE IF NOT EXISTS sellers (
    seller_key TEXT PRIMARY KEY,
    seller TEXT, seller_country TEXT,
    brands TEXT, brand_count INTEGER,
    product_count INTEGER, parent_level_revenue REAL, parent_level_sales INTEGER,
    avg_rating REAL, active_sellers INTEGER
);

CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand_key);
CREATE INDEX IF NOT EXISTS idx_products_seller ON products(seller_key);
"""


def norm_key(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the scan workers write while the dashboard reads, and lets two
    # scan subprocesses commit concurrently without "database is locked".
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    ensure_columns(conn)
    conn.commit()


def ensure_columns(conn):
    """Add columns introduced after a DB was first created (lightweight migration)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(brands)")}
    for col, decl in (
        ("website_urls", "TEXT"), ("website_remarks", "TEXT"),
        ("parent_level_sales", "INTEGER"),
        ("example_url", "TEXT"), ("example_asin", "TEXT"), ("example_title", "TEXT"),
        ("is_green", "INTEGER"),
        # Freshness bookkeeping: per-source scan timestamps drive the 30-day
        # "don't re-scan a signal we already have" rule (see brand_scan.py).
        ("created_at", "TEXT"), ("updated_at", "TEXT"),
        ("website_scanned_at", "TEXT"), ("meta_scanned_at", "TEXT"),
        ("google_scanned_at", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE brands ADD COLUMN {col} {decl}")
    have_s = {r[1] for r in conn.execute("PRAGMA table_info(sellers)")}
    if "parent_level_sales" not in have_s:
        conn.execute("ALTER TABLE sellers ADD COLUMN parent_level_sales INTEGER")
    conn.commit()


def aggregate_sellerbrand(conn, col):
    """Distinct `col` per (seller, brand) -> avoids double counting a parent-level
    figure repeated across child ASINs. Returns {(seller_key, brand_key): sum}."""
    assert col in ("parent_level_revenue", "parent_level_sales"), col
    rows = conn.execute(f"""
        SELECT seller_key, brand_key, {col} AS v
        FROM products
        WHERE {col} IS NOT NULL AND {col} > 0
        GROUP BY seller_key, brand_key, {col}
    """).fetchall()
    out = {}
    for r in rows:
        k = (r["seller_key"], r["brand_key"])
        out[k] = out.get(k, 0.0) + (r["v"] or 0.0)
    return out


def aggregate_sellerbrand_revenue(conn):
    """Back-compat wrapper -> distinct parent_level_revenue per (seller, brand)."""
    return aggregate_sellerbrand(conn, "parent_level_revenue")
