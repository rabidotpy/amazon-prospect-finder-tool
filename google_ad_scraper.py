#!/usr/bin/env python3
"""Brand name -> Google advertiser -> Ads Transparency Center -> ad count.

Companion to meta_ad_scraper.py. Same contract: CODE drives the browser
(Playwright) and does all the looking; the model only JUDGES reduced text and
ABSTAINS when unsure. A wrong advertiser is worse than no advertiser.

Why Google is easier than Meta: Google's Ads Transparency Center is keyed
DIRECTLY on the advertiser's web DOMAIN

    https://adstransparency.google.com/?region=anywhere&domain=<domain>

and (unlike Meta) has no login wall. We already solve brand -> official domain
for the Meta scraper (shared in brand_web.py), so STEP 1 is mostly done:

  STEP 1  Resolve the brand to a Google advertiser.
          ROUTE A (highest confidence): brand -> own web domain (brand_web) ->
            load the domain view of the Transparency Center. The domain IS the
            advertiser key, so provenance is unambiguous — no AI needed. A domain
            can map to MULTIPLE advertiser accounts; the domain view AGGREGATES
            them, which is exactly the per-brand signal we want.
          ROUTE B (fallback, no domain found): search the brand NAME in the
            Transparency Center, collect the advertiser suggestions, and let the
            google-advertiser-disambiguate skill pick the brand's account. Open
            that advertiser's page. Abstains when unsure.

  STEP 2  Count the ads the advertiser runs. The grid lazy-loads creative cards
          on scroll; we scroll until the card count stops growing and count the
          rendered `creative-preview` cards (a trustworthy LOWER BOUND — Google
          shows no total-count label). We also capture the internal
          SearchCreatives XHR JSON to harvest the advertiser ids behind the
          domain and to cross-check the card count. Returns an int when
          confident, or None ("unknown", never a guess / never treated as zero).

Output (per run): brand, domain, advertiser_ids, resolved_via, status,
ads_count, ads[]. Appended one line to data/google_resolutions.jsonl; the full
per-case payload is written to data/<brand>_google_ads.json.

Usage:
    python3 google_ad_scraper.py "caraway"
    HEADFUL=1 python3 google_ad_scraper.py "ridge"     # watch it run (debug)

Requires: pip install playwright && python3 -m playwright install chromium
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

import ai_resolve  # narrow AI decision point (advertiser disambiguation)
from brand_web import UA, clean_brand, _dom, brand_domains

TC_DOMAIN_URL = "https://adstransparency.google.com/?region=anywhere&domain={domain}"
TC_ADVERTISER_URL = "https://adstransparency.google.com/advertiser/{aid}?region=anywhere"
TC_HOME_URL = "https://adstransparency.google.com/?region=anywhere"

# Google's ids in the SearchCreatives JSON: advertisers are AR<digits>,
# creatives are CR<digits>. Generous lower bounds on length to avoid matching
# unrelated tokens.
ADVERTISER_ID_RE = re.compile(r"\bAR\d{10,}\b")
CREATIVE_ID_RE = re.compile(r"\bCR\d{8,}\b")

# Card selectors, most specific first; we take the max count across them.
CARD_SELECTORS = ("creative-preview",
                  "priority-creative-grid creative-preview",
                  "[role='listitem']")

# Google's OWN total label on the domain/advertiser view, e.g. "0 ads",
# "~37 ads", "~3k ads", "1,234 ads". This is authoritative — far better than
# counting lazy-loaded cards — so we parse it as the headline count when present.
TOTAL_ADS_RE = re.compile(r"(~)?\s*([\d][\d,\.]*)\s*([kKmM])?\s+ads\b")


def _parse_total_ads(body):
    """Parse Google's displayed total ("N ads") into (count, approx) or
    (None, False) when no such label is present. `approx` is True for the
    "~3k"/"~37" rounded form."""
    m = TOTAL_ADS_RE.search(body or "")
    if not m:
        return None, False
    approx = bool(m.group(1))
    try:
        num = float(m.group(2).replace(",", ""))
    except ValueError:
        return None, False
    suf = (m.group(3) or "").lower()
    if suf == "k":
        num *= 1000
    elif suf == "m":
        num *= 1_000_000
    return int(round(num)), approx


def _dom_card_count(page):
    best = 0
    for sel in CARD_SELECTORS:
        try:
            best = max(best, len(page.query_selector_all(sel)))
        except Exception:
            pass
    return best


def _dismiss_consent(page):
    """Google may show an EU cookie/consent dialog. Best-effort dismiss."""
    for label in ("Accept all", "Reject all", "I agree", "Accept",
                  "Agree to all", "Allow all"):
        try:
            page.get_by_role("button", name=label).first.click(timeout=1500)
            return
        except Exception:
            pass


# ------------------------------------------------------- STEP 2: scrape ads

def scrape_transparency(page, url, max_scrolls=None):
    """Load a Transparency Center URL (domain view OR advertiser view), lazy-load
    every creative card, and return (count, advertiser_ids, note).

    count is a LOWER-BOUND int when confident, 0 when the page explicitly says
    there are no ads, or None when we can't get a trustworthy number. None means
    UNKNOWN — callers must never treat it as zero. advertiser_ids are the AR…
    ids harvested from the captured SearchCreatives JSON (the accounts behind a
    domain)."""
    if max_scrolls is None:
        max_scrolls = int(os.environ.get("MAX_SCROLLS", "60"))

    blobs = []

    def on_response(resp):
        try:
            if "SearchCreatives" in resp.url or "SearchAds" in resp.url:
                blobs.append(resp.text())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        page.remove_listener("response", on_response)
        return None, [], "goto failed"
    _dismiss_consent(page)
    page.wait_for_timeout(4000)

    # Scroll until the rendered card count stops growing (full grid loaded).
    stable, last = 0, -1
    for _ in range(max_scrolls):
        cards = _dom_card_count(page)
        if cards == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable, last = 0, cards
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(1200)

    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    dom_cards = _dom_card_count(page)
    page.remove_listener("response", on_response)

    text_all = "\n".join(b for b in blobs if b)
    advertiser_ids = sorted(set(ADVERTISER_ID_RE.findall(text_all)))
    creative_ids = set(CREATIVE_ID_RE.findall(text_all))

    # BEST signal: Google's own total label ("N ads"), which the domain/advertiser
    # view always renders — "0 ads" (a trustworthy explicit zero) up to "~3k ads".
    # This is authoritative, so it wins over card/creative counting.
    total, approx = _parse_total_ads(body)
    if total is not None:
        tag = f"~{total}" if approx else str(total)
        return total, advertiser_ids, \
            f"label={tag} cards={dom_cards} json_creatives={len(creative_ids)}"

    # No label parsed (page likely failed to load its data). Fall back to the
    # lazy-loaded card / JSON-creative LOWER BOUNDS — but only as POSITIVE
    # evidence. With neither cards nor creatives AND no label, we have nothing
    # trustworthy: return None (UNKNOWN), never a guessed zero.
    if dom_cards == 0 and not creative_ids:
        return None, advertiser_ids, "no label, no cards, no creatives (unknown)"
    count = max(dom_cards, len(creative_ids))
    return count, advertiser_ids, f"no label; cards={dom_cards} json_creatives={len(creative_ids)}"


# ----------------------------------------------------- STEP 1: resolve advertiser

def resolve_via_domain(page, brand, products):
    """ROUTE A: brand -> own web domain (brand_web) -> Transparency Center domain
    view. Returns a result dict on the first domain that yields a trustworthy
    count (including a genuine 0), else None. The domain is the advertiser key,
    so this needs no AI judgment."""
    doms, note = brand_domains(brand, products)
    if not doms:
        return None
    # Was the domain AI-verified/-discovered, or just a deterministic bare-name
    # TLD guess? An UNVERIFIED domain can belong to a DIFFERENT entity (caraway.com
    # is not the carawayhome.com cookware brand), and on Google a wrong domain
    # silently shows "0 ads" — a false green. We never trust an unverified zero.
    verified = "AI-" in (note or "")

    # Prefer a domain that actually RUNS ads: a positive count is strong evidence
    # the domain is really the brand's advertising domain, whereas a same-name
    # zero may be the wrong entity. So scan candidates and keep the best (>0)
    # hit; only fall back to a zero/unknown if none advertise.
    best_pos = None
    zero_hit = None
    for d in doms:
        url = TC_DOMAIN_URL.format(domain=urllib.parse.quote(d, safe=""))
        count, aids, snote = scrape_transparency(page, url)
        if count is None:
            continue
        rec = {"domain": d, "advertiser_ids": aids, "ads_count": count,
               "resolved_via": "domain", "resolved_url": url,
               "domain_verified": verified, "note": f"{d} [{note}] {snote}"}
        if count > 0:
            best_pos = rec
            break
        if zero_hit is None:
            zero_hit = rec
    if best_pos:
        return best_pos
    if zero_hit:
        return zero_hit
    # Every candidate domain returned UNKNOWN (not a confident 0). Don't claim a
    # resolution — let Route B / unresolved handle it.
    return {"domain": doms[0], "advertiser_ids": [], "ads_count": None,
            "resolved_via": "domain", "domain_verified": verified,
            "resolved_url": TC_DOMAIN_URL.format(domain=urllib.parse.quote(doms[0], safe="")),
            "note": f"{len(doms)} domain(s) returned unknown [{note}]",
            "_unknown": True}


def _collect_advertiser_suggestions(page, brand):
    """Type the brand name into the Transparency Center search and read the
    advertiser suggestion rows as links to /advertiser/<AR…>. Returns
    [{"i","advertiserId","name","region","domains"}]."""
    try:
        page.goto(TC_HOME_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        return []
    _dismiss_consent(page)
    page.wait_for_timeout(3000)

    box = None
    for finder in (
        lambda: page.get_by_placeholder(re.compile(r"search", re.I)),
        lambda: page.locator("input[type='text']"),
        lambda: page.locator("input"),
    ):
        try:
            loc = finder().first
            loc.wait_for(state="visible", timeout=4000)
            box = loc
            break
        except Exception:
            continue
    if box is None:
        return []
    try:
        box.click(timeout=3000)
        box.fill("")
        box.type(brand, delay=70)
        page.wait_for_timeout(3000)
    except Exception:
        return []

    rows, seen = [], set()
    try:
        anchors = page.query_selector_all("a[href*='/advertiser/']")
    except Exception:
        anchors = []
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            m = re.search(r"/advertiser/(AR\d+)", href)
            if not m:
                continue
            aid = m.group(1)
            if aid in seen:
                continue
            name = " ".join((a.inner_text() or "").split())
            if not name:
                continue
            seen.add(aid)
            rows.append({"advertiserId": aid, "name": name,
                         "region": "", "domains": []})
        except Exception:
            continue
        if len(rows) >= 15:
            break
    return rows


def resolve_via_name(page, brand, products):
    """ROUTE B (fallback when no domain): search the brand NAME, AI-disambiguate
    the advertiser suggestions, then scrape that advertiser's page. Abstains
    (returns None) when the AI can't confirm exactly one advertiser."""
    cands = _collect_advertiser_suggestions(page, brand)
    if not cands:
        return None
    rows = [dict(i=i, **c) for i, c in enumerate(cands)]
    if len(rows) == 1:
        chosen = 0
        why = "only candidate"
    else:
        idx, why = ai_resolve.disambiguate_advertisers(brand, products, "", rows)
        if idx is None or idx < 0 or idx >= len(rows):
            return None
        chosen = idx
    aid = rows[chosen]["advertiserId"]
    url = TC_ADVERTISER_URL.format(aid=aid)
    count, aids, snote = scrape_transparency(page, url)
    # The advertiser-id provenance (we opened THIS advertiser's own page) is a
    # trustworthy "this is the brand" signal, so a zero here is verified.
    return {"domain": "", "advertiser_ids": aids or [aid], "ads_count": count,
            "resolved_via": "name", "domain_verified": True,
            "resolved_url": url,
            "note": f"AI-picked {rows[chosen]['name']} ({why}) {snote}"}


# -------------------------------------------------------------------- main

def run(brand, headless=True, domain=None):
    """Resolve the brand's Google advertiser and count its ads.

    When `domain` is supplied (e.g. the brands table already resolved the brand's
    official website during enrichment), STEP 1 is skipped: we load that domain's
    Transparency Center view directly. A caller-supplied domain is treated as
    VERIFIED (it came from enrichment/AI), so a genuine 0 there is a confident
    green — this is the fast-path the dashboard's 'Process Google ads' job uses."""
    domain = _dom(domain) if domain else None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        try:
            print(f"[1/2] resolving Google advertiser for '{brand}' ...", file=sys.stderr)
            products = ai_resolve.product_titles(brand)  # niche context for the AI step
            if products:
                print(f"      product context: {len(products)} title(s) from DB",
                      file=sys.stderr)
            qbrand = clean_brand(brand)  # strip 'LLC' etc. for searching
            if qbrand != brand.strip():
                print(f"      search name: '{qbrand}' (legal suffix stripped)",
                      file=sys.stderr)

            if domain:
                # FAST-PATH: caller already knows the brand's official domain.
                url = TC_DOMAIN_URL.format(domain=urllib.parse.quote(domain, safe=""))
                cnt, aids, snote = scrape_transparency(page, url)
                print(f"      using provided domain {domain} (ids: {aids or 'n/a'})",
                      file=sys.stderr)
                res = {"domain": domain, "advertiser_ids": aids, "ads_count": cnt,
                       "resolved_via": "domain", "domain_verified": True,
                       "resolved_url": url, "note": f"{domain} [provided] {snote}"}
                if cnt is None:
                    res["_unknown"] = True
            else:
                # ROUTE A: brand -> own domain -> Transparency Center domain view.
                res = resolve_via_domain(page, qbrand, products)
            if res and not res.get("_unknown"):
                print(f"      advertiser via domain {res['domain']} "
                      f"(ids: {res['advertiser_ids'] or 'n/a'})", file=sys.stderr)
            else:
                # ROUTE B: name search + AI disambiguation.
                why = (res or {}).get("note", "no domain candidate")
                print(f"      route A inconclusive ({why}); trying name search ...",
                      file=sys.stderr)
                res_b = resolve_via_name(page, qbrand, products)
                if res_b and res_b.get("ads_count") is not None:
                    res = res_b
                    print(f"      advertiser via name search "
                          f"({res['note']})", file=sys.stderr)
                elif res_b:
                    res = res_b  # resolved an advertiser but count unknown
                elif res is None:
                    res = None

            if res is None:
                # No domain candidate AND name search abstained/empty. A
                # resolution gap — NOT proof of zero ads.
                return {"brand": brand, "domain": None, "advertiser_ids": [],
                        "resolved_via": None, "status": "unresolved",
                        "ads_count": None, "ads": [],
                        "error": "could not resolve a Google advertiser"}

            count = res.get("ads_count")
            verified = res.get("domain_verified", False)
            # Status taxonomy mirrors the Meta scraper. Unknown is NEVER zero.
            if count is None:
                status = "resolved_unknown_count"
            elif count == 0:
                # A zero is a green-prospect signal ONLY if we trust the domain.
                # An unverified bare-name domain (deterministic TLD guess) may be
                # a DIFFERENT entity, so its "0 ads" is not a confident green —
                # flag it so downstream never reads it as a clean green prospect.
                status = "resolved_no_ads" if verified else "resolved_no_ads_unverified"
            else:
                status = "resolved"

            if status == "resolved_no_ads_unverified":
                print(f"      WARNING: 0 ads from UNVERIFIED domain "
                      f"{res.get('domain')} — may be the wrong entity (false "
                      f"green); needs AI domain verification", file=sys.stderr)

            return {"brand": brand,
                    "domain": res.get("domain"),
                    "advertiser_ids": res.get("advertiser_ids", []),
                    "resolved_via": res.get("resolved_via"),
                    "domain_verified": verified,
                    "status": status,
                    "transparency_url": res.get("resolved_url"),
                    "resolved_note": res.get("note", ""),
                    "ads_count": count,
                    "ads": []}
        finally:
            browser.close()


def log_resolution(result):
    """Append one compact line per run to data/google_resolutions.jsonl — the
    durable, queryable index over all Google runs (mirrors resolutions.jsonl)."""
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "brand": result.get("brand"),
        "domain": result.get("domain"),
        "advertiser_ids": result.get("advertiser_ids"),
        "resolved_via": result.get("resolved_via"),
        "domain_verified": result.get("domain_verified"),
        "status": result.get("status"),
        "ads_count": result.get("ads_count"),
        "transparency_url": result.get("transparency_url"),
        "error": result.get("error"),
    }
    try:
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", "google_resolutions.jsonl"), "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"      (could not append google_resolutions.jsonl: {e})", file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        print("usage: python3 google_ad_scraper.py <brand>", file=sys.stderr)
        sys.exit(1)
    brand = " ".join(sys.argv[1:])
    result = run(brand, headless=os.environ.get("HEADFUL") != "1")

    os.makedirs("data", exist_ok=True)
    out = os.path.join("data", re.sub(r"\W+", "_", brand.lower()) + "_google_ads.json")
    with open(out, "w") as fh:
        json.dump(result, fh, indent=2)
    log_resolution(result)

    print("\n=== RESULT ===")
    print(f"brand          : {result['brand']}")
    print(f"domain         : {result.get('domain')}")
    print(f"status         : {result.get('status')}")
    if result.get("resolved_via"):
        print(f"resolved via   : {result.get('resolved_via')}")
        print(f"advertiser ids : {result.get('advertiser_ids')}")
        print(f"transparency   : {result.get('transparency_url')}")
        print(f"ads count      : {result.get('ads_count')}  "
              f"({'lower bound' if result.get('ads_count') else result.get('status')})")
        print(f"note           : {result.get('resolved_note')}")
    else:
        print(f"error          : {result.get('error')}")
    print(f"\nfull JSON -> {out}")


if __name__ == "__main__":
    main()
