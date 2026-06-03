#!/usr/bin/env python3
"""Ingest Helium10 exports into SQLite, then (re)build brands + sellers.

Stages:
  1. ingest  - new *blackBoxProducts*.csv in ~/Downloads -> products; rename file *_processed
  2. brands  - aggregate unique brands; enrich website/socials/page_id; scrape ad counts
  3. sellers - aggregate unique sellers

Usage:
  python3 pipeline.py --ingest-only
  python3 pipeline.py                 # ingest + rebuild (enrich + scrape)
  python3 pipeline.py --no-enrich     # rebuild from products only, no web
  python3 pipeline.py --no-scrape     # enrich but skip ad-count scraping
  python3 pipeline.py --limit 25      # cap brands enriched (testing)
"""

import argparse
import csv
import glob
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime

import config
import db
import green

DOWNLOADS = os.path.expanduser("~/Downloads")

# Reuse the proven resolution code from the installed skill (single source).
_SKILL = os.path.expanduser("~/.claude/skills/helium10-prospects/prospect_pipeline.py")
_spec = importlib.util.spec_from_file_location("h10", _SKILL)
h10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h10)

try:
    import adscrape
except Exception:
    adscrape = None

import websearch

try:
    import ai_resolve
except Exception:
    ai_resolve = None


def meta_link_by_page_v2(pid):
    """Page-transparency Ad Library link (active ads, sorted by impressions)."""
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            "&country=ALL&is_targeted_country=false&media_type=all&search_type=page"
            "&sort_data[mode]=total_impressions&sort_data[direction]=desc"
            f"&source=page-transparency-widget&view_all_page_id={pid}")


def to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


# ---------------------------------------------------------------- ingest

IMPORTS_DIR = os.path.join(os.path.dirname(db.DB_PATH), "imports")


def matching_files(include_processed=False):
    """Helium10 Black Box exports (CSV or XLSX) waiting in ~/Downloads."""
    out = []
    for ext in ("csv", "xlsx", "xls"):
        for f in glob.glob(os.path.join(DOWNLOADS, f"*.{ext}")):
            b = os.path.basename(f).lower()
            if "amazon" in b and "blackboxproducts" in b:
                if include_processed or "_processed" not in b:
                    out.append(f)
    return sorted(out, key=os.path.getmtime)


def read_records(path):
    """Yield raw header->value dicts from a Helium10 export, CSV or XLSX."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        import pandas as pd
        df = pd.read_excel(path, dtype=str).fillna("")
        for _, r in df.iterrows():
            yield {k: ("" if v is None else str(v)) for k, v in r.items()}
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                yield r


COLMAP = {
    "url": "URL", "image_url": "Image URL", "asin": "ASIN", "title": "Title",
    "brand": "Brand", "fulfillment": "Fulfillment", "category": "Category",
    "subcategory": "Subcategory", "price": "Price", "seller": "Seller",
    "seller_country": "Seller Country/Region",
}
INTCOLS = {"bsr": "BSR", "subcategory_bsr": "Subcategory BSR",
           "asin_sales": "ASIN Sales", "parent_level_sales": "Parent Level Sales",
           "review_count": "Review Count", "active_sellers": "Number of Active Sellers",
           "number_of_images": "Number of Images", "variation_count": "Variation Count"}
FLOATCOLS = {"asin_revenue": "ASIN Revenue", "parent_level_revenue": "Parent Level Revenue",
             "reviews_rating": "Reviews Rating", "listing_age_months": "Listing Age (Months)"}


def ingest_file(conn, path):
    """Upsert every product row from `path` keyed on ASIN (UNIQUE(asin) means a
    repeated ASIN refreshes its row rather than duplicating). Returns a dict
    {inserted, updated, skipped} so the caller can report import results."""
    src = os.path.basename(path)
    now = datetime.now().isoformat(timespec="seconds")
    have = {r[0] for r in conn.execute("SELECT asin FROM products")}
    inserted = updated = skipped = 0
    for r in read_records(path):
        row = {k: (r.get(v) or "").strip() for k, v in COLMAP.items()}
        for k, v in INTCOLS.items():
            row[k] = to_int(r.get(v))
        for k, v in FLOATCOLS.items():
            row[k] = to_float(r.get(v))
        row["brand_key"] = db.norm_key(row["brand"])
        row["seller_key"] = db.norm_key(row["seller"])
        row["source_file"] = src
        row["imported_at"] = now
        if not row["asin"]:
            skipped += 1
            continue
        cols = ",".join(row.keys())
        ph = ",".join("?" for _ in row)
        try:
            conn.execute(f"INSERT OR REPLACE INTO products ({cols}) VALUES ({ph})",
                         list(row.values()))
            if row["asin"] in have:
                updated += 1
            else:
                inserted += 1
                have.add(row["asin"])
        except Exception as e:
            skipped += 1
            print("  row error:", e, file=sys.stderr)
    conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def ingest_new(conn):
    """Ingest every un-processed Helium10 export (CSV or XLSX) in Downloads.
    Returns (files_ingested, rows_inserted)."""
    files = matching_files()
    total = 0
    for f in files:
        res = ingest_file(conn, f)
        new = mark_processed(f)
        total += res["inserted"]
        print(f"ingested {res} from {os.path.basename(f)} -> {os.path.basename(new)}",
              file=sys.stderr)
    return len(files), total


def ingest_path(conn, path, rebuild=True):
    """Ingest a single export file (any location). Optionally refresh the brand
    skeleton + sellers so new products surface immediately. Returns the
    {inserted, updated, skipped} dict from ingest_file."""
    res = ingest_file(conn, path)
    if rebuild:
        sync_brand_skeleton(conn)
        rebuild_sellers(conn)
    return res


def ingest_upload(conn, filename, data, rebuild=True):
    """Persist an uploaded export (bytes) under data/imports/, then ingest it.
    Used by the dashboard's 'Add new products' uploader."""
    os.makedirs(IMPORTS_DIR, exist_ok=True)
    safe = os.path.basename(filename) or "upload"
    dest = os.path.join(IMPORTS_DIR, datetime.now().strftime("%Y%m%d_%H%M%S_") + safe)
    with open(dest, "wb") as fh:
        fh.write(data)
    res = ingest_path(conn, dest, rebuild=rebuild)
    res["saved_as"] = dest
    return res


def mark_processed(path):
    base, ext = os.path.splitext(path)
    if base.endswith("_processed"):
        return path
    new = base + "_processed" + ext
    try:
        os.rename(path, new)
        return new
    except Exception as e:
        print("  rename failed:", e, file=sys.stderr)
        return path


# ---------------------------------------------------------- brand aggregation

def prospect_signal(min_age, total_reviews, revenue):
    new = bool(min_age) and min_age <= 6
    strong = (total_reviews or 0) >= 100 or (revenue or 0) >= 50000
    if strong and new:
        return "HOT_new_and_selling"
    if strong:
        return "established_seller"
    if new:
        return "new_listing"
    return "watch"


def brand_aggregates(conn):
    sb_rev = db.aggregate_sellerbrand_revenue(conn)  # (seller,brand)->rev
    sb_sales = db.aggregate_sellerbrand(conn, "parent_level_sales")
    brand_rev, brand_sales, brand_sellers = {}, {}, {}
    for (sk, bk), rev in sb_rev.items():
        brand_rev[bk] = brand_rev.get(bk, 0.0) + rev
        brand_sellers.setdefault(bk, set()).add(sk)
    for (sk, bk), s in sb_sales.items():
        brand_sales[bk] = brand_sales.get(bk, 0.0) + s

    rows = conn.execute("""
        SELECT brand_key, MAX(brand) brand, COUNT(*) asin_count,
               SUM(review_count) total_reviews,
               MIN(NULLIF(listing_age_months,0)) min_age,
               MAX(fulfillment) fulfillment,
               MAX(title) example_title, MAX(asin) example_asin, MAX(url) example_url
        FROM products GROUP BY brand_key
    """).fetchall()
    out = []
    for r in rows:
        bk = r["brand_key"]
        if not bk:
            continue
        out.append({
            "brand_key": bk, "brand": r["brand"],
            "asin_count": r["asin_count"], "total_reviews": r["total_reviews"] or 0,
            "min_listing_age": r["min_age"] or 0,
            "parent_level_revenue": round(brand_rev.get(bk, 0.0), 2),
            "parent_level_sales": int(brand_sales.get(bk, 0)),
            "seller_count": len(brand_sellers.get(bk, [])),
            "example_title": r["example_title"], "example_asin": r["example_asin"],
            "example_url": r["example_url"],
        })
    out.sort(key=lambda x: x["parent_level_revenue"], reverse=True)
    return out


# Web-resolution fields are cacheable (a brand's site/socials/page_id rarely
# change). Ad counts are NOT cached here — they drift, so we re-scrape each run.
WEB_FIELDS = ("website_url", "website_urls", "website_remarks", "meta_ads_url",
              "google_ads_url", "fb_page_id", "facebook", "instagram", "tiktok",
              "youtube", "meta_link_kind", "website_source", "confidence")


def _as_list(v):
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _from_cache(c):
    """Normalize a cache entry to WEB_FIELDS. The skill writes keys `website`,
    `meta_ad_library`, `google_ads`; the pipeline writes `*_url`. Accept both."""
    website = c.get("website_url") or c.get("website") or ""
    urls = _as_list(c.get("website_urls"))
    if not urls and website:
        urls = ["https://" + website.replace("https://", "").replace("http://", "")]
    return {
        "website_url": website,
        "website_urls": urls,
        "website_remarks": c.get("website_remarks") or "",
        "meta_ads_url": c.get("meta_ads_url") or c.get("meta_ad_library") or "",
        "google_ads_url": c.get("google_ads_url") or c.get("google_ads") or "",
        "fb_page_id": c.get("fb_page_id") or "",
        "facebook": c.get("facebook") or "",
        "instagram": c.get("instagram") or "",
        "tiktok": c.get("tiktok") or "",
        "youtube": c.get("youtube") or "",
        "meta_link_kind": c.get("meta_link_kind") or "",
        "website_source": c.get("website_source") or "cache",
        "confidence": c.get("confidence") or ("cache" if website else "none"),
    }


def _to_cache(web):
    """Write BOTH key spellings so the skill and pipeline stay interoperable."""
    return {
        "website": web["website_url"], "website_url": web["website_url"],
        "website_urls": web["website_urls"], "website_remarks": web["website_remarks"],
        "website_source": web["website_source"], "confidence": web["confidence"],
        "facebook": web["facebook"], "instagram": web["instagram"],
        "tiktok": web["tiktok"], "youtube": web["youtube"],
        "fb_page_id": web["fb_page_id"],
        "meta_ad_library": web["meta_ads_url"], "meta_ads_url": web["meta_ads_url"],
        "meta_link_kind": web["meta_link_kind"],
        "google_ads": web["google_ads_url"], "google_ads_url": web["google_ads_url"],
    }


def resolve_web(rec, use_enrich):
    """Website/socials/page_id resolution.

    Division of labour:
      • DETERMINISTIC = context only. websearch.find_websites() gathers candidate
        domains (brand-name probes + Google hits). These are NEVER taken as the
        answer — guessing pollutes the data and forces cleanup later.
      • AI = the decision. The meta-website-verify skill (claude CLI) picks the
        brand's real site from the candidates using the products it sells, or
        abstains. Only an AI decision is written.

    `website_resolved` in the result says whether AI actually decided:
      True  -> AI picked a site, or AI confidently said there is none. Persist +
               stamp website_scanned_at (it's settled).
      False -> AI was unavailable/unauthenticated. Persist NOTHING and DON'T stamp,
               so the brand stays pending and is retried once AI is back. We never
               write a deterministic guess.
    """
    website = meta_url = google_url = fb = ig = tt = yt = ""
    page_id = meta_kind = ""
    src = conf = "skipped"
    website_urls = []
    remarks = ""
    resolved = True            # nothing to decide unless we actually search
    if use_enrich:
        cands = websearch.find_websites(rec["brand"], context=rec.get("example_title", ""))
        website_urls = [c["url"] for c in cands]   # deterministic CONTEXT for AI

        cand_dicts = [{"domain": websearch.domain_of(c["url"]),
                       "title": c.get("title", ""),
                       "snippet": c.get("snippet", "")} for c in cands]
        products = ai_resolve.product_titles(rec["brand"]) if ai_resolve else []
        if ai_resolve is not None:
            site, why, ok = ai_resolve.verify_website(
                rec["brand"], products, cand_dicts)
        else:
            site, why, ok = None, "ai_resolve missing", False

        if ok and site:                       # AI picked the real site
            website = websearch.domain_of(
                site if site.startswith("http") else "https://" + site) or site
            src, conf = "ai_verified", "ai"
            remarks = f"AI-verified from {len(cands)} candidate(s): {why}"
        elif ok:                              # AI ran and confidently found none
            website, src, conf = "", "ai_abstain", "none"
            remarks = f"AI: no real site ({len(cands)} candidate(s)): {why}"
        else:                                 # AI unavailable -> leave PENDING
            resolved = False
            website, src, conf = "", "ai_unavailable", "pending"
            remarks = (f"AI unavailable ({why}); {len(cands)} candidate(s) "
                       f"gathered, website left pending for retry.")

        if website:
            time.sleep(h10.REQUEST_DELAY)
            socials = h10.fetch_socials(website)
            fb, ig, tt, yt = socials["facebook"], socials["instagram"], socials["tiktok"], socials["youtube"]
            if fb:
                time.sleep(h10.REQUEST_DELAY)
                page_id = h10.resolve_fb_page_id(fb) or ""
            google_url = h10.google_link_by_domain(website)
        meta_url = meta_link_by_page_v2(page_id) if page_id else ""
        meta_kind = "page_direct" if page_id else ""
    return {
        "website_url": website, "website_urls": website_urls,
        "website_remarks": remarks,
        "meta_ads_url": meta_url, "google_ads_url": google_url,
        "fb_page_id": page_id, "facebook": fb, "instagram": ig, "tiktok": tt,
        "youtube": yt, "meta_link_kind": meta_kind,
        "website_source": src, "confidence": conf,
        "website_resolved": resolved,
    }


def scrape_counts(web):
    """Scrape Meta/Google ad counts from the resolved page_id/website.
    Returns counts + is_* flags. None = unknown (not scraped / not confident)."""
    meta_count = google_count = None
    if adscrape:
        if web.get("fb_page_id"):
            meta_count = adscrape.meta_ad_count(web["fb_page_id"])
        if web.get("website_url"):
            google_count = adscrape.google_ad_count(web["website_url"])
    is_meta = (1 if (meta_count or 0) > 0 else 0) if meta_count is not None else None
    is_google = (1 if (google_count or 0) > 0 else 0) if google_count is not None else None
    return {"meta_ads_count": meta_count, "google_ads_count": google_count,
            "is_meta_ads": is_meta, "is_google_ads": is_google}


BRAND_COLS = ["brand_key", "brand", "website_url", "website_urls", "website_remarks",
              "has_website",
              "meta_ads_url", "google_ads_url", "meta_ads_count", "google_ads_count",
              "is_meta_ads", "is_google_ads", "meta_link_kind", "fb_page_id", "facebook",
              "instagram", "tiktok", "youtube", "parent_level_revenue",
              "parent_level_sales", "total_reviews", "asin_count", "seller_count",
              "min_listing_age", "prospect_signal", "confidence",
              "example_url", "example_asin", "example_title", "is_green", "enriched_at"]

# Aggregate (products-derived) fields, refreshed every run without re-enriching.
AGG_COLS = ["brand", "parent_level_revenue", "parent_level_sales", "total_reviews",
            "asin_count", "seller_count", "min_listing_age", "prospect_signal",
            "example_url", "example_asin", "example_title"]


def sync_brand_skeleton(conn):
    """Refresh products-derived aggregate fields for every brand. New brands are
    inserted with enriched_at=NULL (pending web enrichment); existing brands keep
    their enrichment but get fresh revenue/sales/review numbers. Returns the count
    of brands still pending enrichment."""
    aggs = brand_aggregates(conn)
    existing = {r["brand_key"] for r in conn.execute("SELECT brand_key FROM brands")}
    for rec in aggs:
        bk = rec["brand_key"]
        sig = prospect_signal(rec["min_listing_age"], rec["total_reviews"],
                              rec["parent_level_revenue"])
        vals = {
            "brand": rec["brand"],
            "parent_level_revenue": rec["parent_level_revenue"],
            "parent_level_sales": rec["parent_level_sales"],
            "total_reviews": rec["total_reviews"], "asin_count": rec["asin_count"],
            "seller_count": rec["seller_count"], "min_listing_age": rec["min_listing_age"],
            "prospect_signal": sig, "example_url": rec["example_url"],
            "example_asin": rec["example_asin"], "example_title": rec["example_title"],
        }
        if bk in existing:
            sets = ",".join(f"{c}=?" for c in AGG_COLS)
            conn.execute(f"UPDATE brands SET {sets} WHERE brand_key=?",
                         [vals[c] for c in AGG_COLS] + [bk])
        else:
            conn.execute(
                "INSERT INTO brands (brand_key, " + ",".join(AGG_COLS) + ", enriched_at) "
                "VALUES (?," + ",".join("?" for _ in AGG_COLS) + ",NULL)",
                [bk] + [vals[c] for c in AGG_COLS])
    conn.commit()
    return conn.execute(
        "SELECT COUNT(*) c FROM brands WHERE enriched_at IS NULL").fetchone()["c"]


def pending_keys(conn, limit=None):
    """Brand keys awaiting enrichment, richest first (best leads enriched first)."""
    q = ("SELECT brand_key FROM brands WHERE enriched_at IS NULL "
         "ORDER BY parent_level_revenue DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r["brand_key"] for r in conn.execute(q).fetchall()]


def _brand_rec(conn, brand_key):
    r = conn.execute("SELECT * FROM brands WHERE brand_key=?", (brand_key,)).fetchone()
    if not r:
        return None
    return {"brand_key": brand_key, "brand": r["brand"],
            "example_title": r["example_title"] or ""}


def enrich_one(conn, brand_key, use_enrich=True, use_scrape=True, cache=None):
    """Enrich a single brand: website/socials/page_id + ad counts, write the row,
    recompute its green flag. Uses the shared cache when supplied. Returns the row."""
    rec = _brand_rec(conn, brand_key)
    if not rec:
        return None
    own_cache = cache is None
    if own_cache and use_enrich:
        cache = h10.load_cache()

    key = rec["brand"].lower().strip()
    if use_enrich and cache is not None and key in cache:
        web = _from_cache(cache[key])
    else:
        web = resolve_web(rec, use_enrich)
        if use_enrich and cache is not None:
            cache[key] = _to_cache(web)

    counts = scrape_counts(web) if use_scrape else {
        "meta_ads_count": None, "google_ads_count": None,
        "is_meta_ads": None, "is_google_ads": None}
    enr = {**web, **counts}

    updates = {
        "website_url": enr.get("website_url"),
        "website_urls": json.dumps(enr.get("website_urls") or []),
        "website_remarks": enr.get("website_remarks"),
        "has_website": 1 if enr.get("website_url") else 0,
        "meta_ads_url": enr.get("meta_ads_url"), "google_ads_url": enr.get("google_ads_url"),
        "meta_ads_count": enr.get("meta_ads_count"), "google_ads_count": enr.get("google_ads_count"),
        "is_meta_ads": enr.get("is_meta_ads"), "is_google_ads": enr.get("is_google_ads"),
        "meta_link_kind": enr.get("meta_link_kind"), "fb_page_id": enr.get("fb_page_id"),
        "facebook": enr.get("facebook"), "instagram": enr.get("instagram"),
        "tiktok": enr.get("tiktok"), "youtube": enr.get("youtube"),
        "confidence": enr.get("confidence"),
        "enriched_at": datetime.now().isoformat(timespec="seconds"),
    }
    sets = ",".join(f"{c}=?" for c in updates)
    conn.execute(f"UPDATE brands SET {sets} WHERE brand_key=?",
                 list(updates.values()) + [brand_key])
    conn.commit()
    green.mark_one(conn, brand_key)
    if own_cache and use_enrich:
        h10.save_cache(cache)
    return updates


def rebuild_brands(conn, use_enrich=True, use_scrape=True, limit=None):
    """One-shot full build: sync skeleton, then enrich every pending brand."""
    sync_brand_skeleton(conn)
    keys = pending_keys(conn, limit=limit)
    cache = h10.load_cache() if use_enrich else None
    done = 0
    for bk in keys:
        up = enrich_one(conn, bk, use_enrich, use_scrape, cache=cache)
        done += 1
        print(f"  [{done}/{len(keys)}] {(up or {}).get('website_url') or 'unknown'}",
              file=sys.stderr)
    if use_enrich and cache is not None:
        h10.save_cache(cache)
    green.recompute_all(conn)
    green.export_csv(conn)
    return done


# --------------------------------------------------------- seller aggregation

def rebuild_sellers(conn):
    sb_rev = db.aggregate_sellerbrand_revenue(conn)
    sb_sales = db.aggregate_sellerbrand(conn, "parent_level_sales")
    seller_rev, seller_sales = {}, {}
    for (sk, bk), rev in sb_rev.items():
        seller_rev[sk] = seller_rev.get(sk, 0.0) + rev
    for (sk, bk), s in sb_sales.items():
        seller_sales[sk] = seller_sales.get(sk, 0.0) + s
    rows = conn.execute("""
        SELECT seller_key, MAX(seller) seller, MAX(seller_country) seller_country,
               COUNT(*) product_count, AVG(reviews_rating) avg_rating,
               MAX(active_sellers) active_sellers,
               GROUP_CONCAT(DISTINCT brand) brands
        FROM products GROUP BY seller_key
    """).fetchall()
    n = 0
    for r in rows:
        sk = r["seller_key"]
        if not sk:
            continue
        brands = sorted(set((r["brands"] or "").split(",")))
        conn.execute("""INSERT OR REPLACE INTO sellers
            (seller_key, seller, seller_country, brands, brand_count, product_count,
             parent_level_revenue, parent_level_sales, avg_rating, active_sellers)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sk, r["seller"], r["seller_country"], ", ".join(brands), len(brands),
             r["product_count"], round(seller_rev.get(sk, 0.0), 2),
             int(seller_sales.get(sk, 0)), round(r["avg_rating"] or 0, 2),
             r["active_sellers"]))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest-only", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--no-scrape", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    conn = db.connect()
    db.init_db(conn)

    nfiles, total_ins = ingest_new(conn)

    prod_count = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]

    if args.ingest_only:
        nb = ns = 0
    else:
        nb = rebuild_brands(conn, use_enrich=not args.no_enrich,
                            use_scrape=not args.no_scrape, limit=args.limit)
        ns = rebuild_sellers(conn)

    print("\n=== pipeline summary ===")
    print(f"files ingested this run : {nfiles}")
    print(f"rows inserted this run  : {total_ins}")
    print(f"products (total)        : {prod_count}")
    if not args.ingest_only:
        print(f"brands rebuilt          : {nb}")
        print(f"sellers rebuilt         : {ns}")
    print(f"database                : {db.DB_PATH}")


if __name__ == "__main__":
    main()
