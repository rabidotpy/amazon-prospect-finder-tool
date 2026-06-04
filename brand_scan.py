#!/usr/bin/env python3
"""Per-brand scanner — the single place that turns a brand row into resolved
website/socials/fb-page + real Meta & Google ad counts.

It replaces the old `adscrape`-based count path: ad counts now come from the
production scrapers (`meta_ad_scraper` / `google_ad_scraper`), which are AI-aware
and abstain instead of guessing.

Freshness ("don't re-scan a signal we already have"):
    Each signal carries its own timestamp on the brands row —
    website_scanned_at / meta_scanned_at / google_scanned_at. A signal is only
    re-scanned when its stamp is missing or older than STALE_DAYS. So a manual
    or background job can be fired repeatedly and will skip brands that were
    scanned in the last 30 days, scanning only what is stale or new.

"Unknown is never zero": when a scraper can't trust a count it returns an
unresolved status and we write None (leave it unknown), never 0. Only a
genuinely-confirmed empty result becomes 0 — this protects the green rule from
false positives.

CLI (used by ad_jobs' concurrency-2 pool — one subprocess per brand, so each
scan gets its own browser + SQLite connection):
    python3 brand_scan.py scan <brand_key> [--force]
Prints a single JSON line describing the outcome.
"""

import datetime as dt
import json
import sys

import config
import db
import green
import pipeline

STALE_DAYS = 30


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _stale(ts, days=STALE_DAYS):
    """True if a timestamp is missing or older than `days` (=> needs scanning)."""
    if not ts:
        return True
    try:
        t = dt.datetime.fromisoformat(ts)
    except Exception:
        return True
    return (dt.datetime.now() - t).days >= days


def _flag(count):
    """1 if running ads, 0 if confirmed none, None if unknown."""
    if count is None:
        return None
    return 1 if count > 0 else 0


# ----------------------------------------------------- result -> brands columns

def apply_meta_result(conn, brand_key, result):
    """Map a meta_ad_scraper.run() result to brands columns. Returns
    (count, status). Honors 'unknown is never zero'."""
    status = result.get("status")
    pid = result.get("page_id")
    if status == "resolved_no_ads":
        count = 0
    elif status == "resolved":
        count = result.get("reported_count")
        if count is None and (result.get("scraped_count") or 0) > 0:
            count = result.get("scraped_count")
    else:
        count = None  # resolved_unknown_count / profile_suspect / unresolved
    conn.execute(
        "UPDATE brands SET meta_ads_count=?, is_meta_ads=?, "
        "fb_page_id=COALESCE(NULLIF(?,''), fb_page_id), "
        "meta_ads_url=COALESCE(?, meta_ads_url), "
        "meta_link_kind=COALESCE(?, meta_link_kind), meta_scanned_at=? "
        "WHERE brand_key=?",
        (count, _flag(count), pid or "",
         result.get("ad_library_url") or None,
         result.get("resolved_via") or None, _now(), brand_key))
    conn.commit()
    return count, status


def apply_google_result(conn, brand_key, result):
    """Map a google_ad_scraper.run() result to brands columns. Returns
    (count, status). An UNVERIFIED zero is treated as unknown (None) so a wrong
    bare-name domain can't manufacture a false green."""
    status = result.get("status")
    if status == "resolved":
        count = result.get("ads_count")
    elif status == "resolved_no_ads":      # verified empty -> genuine 0
        count = 0
    else:
        count = None  # resolved_no_ads_unverified / unknown_count / unresolved
    conn.execute(
        "UPDATE brands SET google_ads_count=?, is_google_ads=?, "
        "google_ads_url=COALESCE(?, google_ads_url), google_scanned_at=? "
        "WHERE brand_key=?",
        (count, _flag(count), result.get("transparency_url") or None,
         _now(), brand_key))
    conn.commit()
    return count, status


# --------------------------------------------------------------- website signal

def _apply_web(conn, brand_key, web):
    """Persist resolve_web() output. fb_page_id is COALESCE'd so a failed social
    re-scan never wipes a page id we already trust."""
    conn.execute(
        "UPDATE brands SET website_url=?, website_urls=?, website_remarks=?, "
        "has_website=?, facebook=?, instagram=?, tiktok=?, youtube=?, "
        "fb_page_id=COALESCE(NULLIF(?,''), fb_page_id), "
        "meta_ads_url=COALESCE(NULLIF(?,''), meta_ads_url), "
        "google_ads_url=COALESCE(NULLIF(?,''), google_ads_url), "
        "meta_link_kind=COALESCE(NULLIF(?,''), meta_link_kind), "
        "confidence=?, website_scanned_at=? WHERE brand_key=?",
        (web.get("website_url") or "", json.dumps(web.get("website_urls") or []),
         web.get("website_remarks") or "",
         1 if web.get("website_url") else 0,
         web.get("facebook") or "", web.get("instagram") or "",
         web.get("tiktok") or "", web.get("youtube") or "",
         web.get("fb_page_id") or "", web.get("meta_ads_url") or "",
         web.get("google_ads_url") or "", web.get("meta_link_kind") or "",
         web.get("confidence") or "", _now(), brand_key))
    conn.commit()


# --------------------------------------------------------------- the scan

_AI_LIVE = {"ts": None, "ok": None}


def ai_live_cached(ttl=300):
    """ai_live() with a short cache so the worker doesn't probe claude every cycle.
    Returns True only if the CLI is actually authenticated."""
    now = dt.datetime.now()
    if (_AI_LIVE["ok"] is not None and _AI_LIVE["ts"]
            and (now - _AI_LIVE["ts"]).total_seconds() < ttl):
        return _AI_LIVE["ok"]
    ok, _ = ai_live()
    _AI_LIVE["ok"], _AI_LIVE["ts"] = ok, now
    return ok


AI_REVENUE_FLOOR = config._float("AI_REVENUE_FLOOR", 1000.0)


def _google_domain(google_ads_url):
    """Extract the advertiser domain from a Google Transparency URL
    (…?domain=legion.co). Empty string if absent."""
    import re
    import urllib.parse
    m = re.search(r"[?&]domain=([^&]+)", google_ads_url or "")
    return urllib.parse.unquote(m.group(1)).strip() if m else ""


def reconcile_website(conn, brand_key):
    """If a brand has NO website on file but the Google ad scraper resolved a
    brand-matching advertiser domain, adopt that domain as the website. The Google
    Transparency advertiser domain is a verified, brand-owned domain, so this is a
    free, high-confidence website signal we'd otherwise throw away — and it stops a
    brand that clearly has a DTC presence from being mislabelled a green prospect.
    Returns the adopted domain, or None."""
    import websearch
    row = conn.execute(
        "SELECT brand, website_url, google_ads_url FROM brands WHERE brand_key=?",
        (brand_key,)).fetchone()
    if not row or (row["website_url"] or "").strip():
        return None
    gdom = _google_domain(row["google_ads_url"])
    if not gdom or not websearch.brand_domain_match(row["brand"], gdom):
        return None
    conn.execute(
        "UPDATE brands SET website_url=?, has_website=1, "
        "confidence='google_domain', website_scanned_at=COALESCE(website_scanned_at,?), "
        "google_ads_url=COALESCE(NULLIF(google_ads_url,''), google_ads_url) "
        "WHERE brand_key=?", (gdom, _now(), brand_key))
    conn.commit()
    return gdom


def stale_brand_keys(conn, limit=None):
    """Brand keys with at least one missing/stale signal, richest first (best
    leads scanned first). Drives the background worker.

    Website is 'determine once', AI-gated AND revenue-gated: a never-resolved
    website only counts as work when the claude CLI is authenticated AND the brand
    clears AI_REVENUE_FLOOR — we spend AI research only on brands worth pitching.
    Meta/Google follow the 30-day freshness rule regardless.
    """
    rows = conn.execute(
        "SELECT brand_key, parent_level_revenue, website_scanned_at, "
        "meta_scanned_at, google_scanned_at "
        "FROM brands ORDER BY parent_level_revenue DESC").fetchall()
    ai_ok = ai_live_cached()
    out = []
    for r in rows:
        rich = (r["parent_level_revenue"] or 0) >= AI_REVENUE_FLOOR
        website_work = (not r["website_scanned_at"]) and ai_ok and rich
        if (website_work or _stale(r["meta_scanned_at"])
                or _stale(r["google_scanned_at"])):
            out.append(r["brand_key"])
            if limit and len(out) >= limit:
                break
    return out


def needs(row, force=False):
    """Which signals to (re)scan for this brand row. Website is determine-once
    (no age check); only --force re-resolves a website we already have."""
    return {
        "website": force or not row["website_scanned_at"],
        "meta": force or _stale(row["meta_scanned_at"]),
        "google": force or _stale(row["google_scanned_at"]),
    }


def scan_one(brand_key, force=False, headless=True, conn=None):
    """Scan a single brand, persisting only the signals that are stale/missing.
    Returns a compact outcome dict. Safe to call repeatedly — fresh signals are
    skipped (see STALE_DAYS)."""
    own = conn is None
    if own:
        conn = db.connect()
        db.init_db(conn)
    row = conn.execute("SELECT * FROM brands WHERE brand_key=?",
                       (brand_key,)).fetchone()
    if not row:
        return {"brand_key": brand_key, "status": "missing", "did": []}

    brand = row["brand"]
    todo = needs(row, force)
    out = {"brand_key": brand_key, "brand": brand, "did": [], "skipped": [],
           "status": "ok"}
    for sig in ("website", "meta", "google"):
        if not todo[sig]:
            out["skipped"].append(sig)

    # 1) website / socials / fb_page_id (seeds the Meta fast-path).
    #    Only persisted+stamped when AI actually DECIDED (resolved=True). If AI is
    #    unavailable the website is left PENDING — never a deterministic guess —
    #    so the brand stays stale and is retried once AI is back.
    allow_ai = force or (row["parent_level_revenue"] or 0) >= AI_REVENUE_FLOOR
    if todo["website"] and not allow_ai:
        out["skipped"].append("website")          # low-value: don't spend AI research
        out["website_status"] = "below_revenue_floor"
    elif todo["website"]:
        try:
            web = pipeline.resolve_web(
                {"brand": brand, "example_title": row["example_title"] or ""},
                use_enrich=True, allow_ai=allow_ai)
            if web.get("website_resolved"):
                _apply_web(conn, brand_key, web)   # stamps website_scanned_at
                out["did"].append("website")
                out["website_url"] = web.get("website_url") or ""
                row = conn.execute("SELECT * FROM brands WHERE brand_key=?",
                                   (brand_key,)).fetchone()
            else:
                out["website_status"] = "pending_ai"  # not stamped -> retried
        except Exception as e:
            out["website_error"] = str(e)

    # 2) Meta ad count (reuse resolved fb_page_id when present)
    if todo["meta"]:
        try:
            import meta_ad_scraper
            res = meta_ad_scraper.run(brand, headless=headless,
                                      page_id=row["fb_page_id"] or None)
            c, s = apply_meta_result(conn, brand_key, res)
            out["did"].append("meta")
            out["meta_count"], out["meta_status"] = c, s
        except Exception as e:
            out["meta_error"] = str(e)

    # 3) Google ad count (reuse resolved website domain when present)
    if todo["google"]:
        try:
            import google_ad_scraper
            res = google_ad_scraper.run(brand, headless=headless,
                                        domain=row["website_url"] or None)
            c, s = apply_google_result(conn, brand_key, res)
            out["did"].append("google")
            out["google_count"], out["google_status"] = c, s
        except Exception as e:
            out["google_error"] = str(e)

    # Cross-signal reconcile: if website research found nothing but the Google ad
    # scraper resolved a brand-matching advertiser domain, adopt it as the website.
    adopted = reconcile_website(conn, brand_key)
    if adopted:
        out["website_url"] = adopted
        out["website_source"] = "google_domain"
        if "website" not in out["did"]:
            out["did"].append("website(from-google)")

    conn.execute(
        "UPDATE brands SET updated_at=?, created_at=COALESCE(created_at,?), "
        "enriched_at=COALESCE(enriched_at,?) WHERE brand_key=?",
        (_now(), _now(), _now(), brand_key))
    conn.commit()
    green.mark_one(conn, brand_key)
    if not out["did"]:
        out["status"] = "fresh"  # nothing was stale; nothing scanned
    return out


def ai_live():
    """Probe whether the claude CLI is actually authenticated (not just present).
    Returns (ok, reason). Makes one tiny real call."""
    try:
        import ai_resolve
    except Exception as e:
        return False, f"ai_resolve import failed: {e}"
    if not ai_resolve.available():
        return False, "claude CLI or meta-website-verify skill not found"
    _, why = ai_resolve.verify_website(
        "ProbeBrand", ["probe product"],
        [{"domain": "example.com", "title": "", "snippet": ""}])
    ok = not ai_resolve.ai_failed(why)
    return (ok, "ok" if ok else "claude -p failed (likely 401/unauthenticated)")


def reresolve_websites(conn=None, force_all=False):
    """Queue websites for AI re-resolution by clearing website_scanned_at, so the
    'determine once' rule re-runs them. GUARDED: aborts unless the claude CLI is
    actually authenticated, so we never blow away good websites only to re-guess
    them without AI. By default only re-does brands NOT already AI-verified."""
    own = conn is None
    if own:
        conn = db.connect()
        db.init_db(conn)
    ok, reason = ai_live()
    if not ok:
        return {"ok": False, "reason": reason, "reset": 0}
    if force_all:
        cur = conn.execute("UPDATE brands SET website_scanned_at=NULL")
    else:
        cur = conn.execute(
            "UPDATE brands SET website_scanned_at=NULL "
            "WHERE COALESCE(confidence,'') <> 'ai'")
    conn.commit()
    return {"ok": True, "reason": "queued", "reset": cur.rowcount}


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "scan":
        force = "--force" in args
        out = scan_one(args[1], force=force)
        print(json.dumps(out))
    elif args and args[0] == "ai-check":
        ok, reason = ai_live()
        print(json.dumps({"ai_authenticated": ok, "reason": reason}))
        sys.exit(0 if ok else 2)
    elif args and args[0] == "reresolve":
        out = reresolve_websites(force_all="--all" in args)
        print(json.dumps(out))
        sys.exit(0 if out["ok"] else 2)
    else:
        print("usage: python3 brand_scan.py "
              "{scan <brand_key> [--force] | ai-check | reresolve [--all]}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
