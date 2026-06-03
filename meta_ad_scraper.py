#!/usr/bin/env python3
"""Brand name -> Facebook page_id -> Meta Ad Library -> every live ad.

Given ONLY a brand string (e.g. "vivonu"), this:

  STEP 1  Resolve the brand's Facebook PAGE ID.
          We open the brand's Facebook page (facebook.com/<handle>) in a real,
          JavaScript-running browser and read the numeric page id out of the
          page's own metadata. (Facebook's vanity URLs hide the numeric id from
          the address bar, but it's always embedded in the page: in the
          `al:android:url` / `al:ios:url` app-link meta tags as fb://page/?id=...,
          and in inline JSON like "pageID":"...". That's the id we need.)
          If the vanity page can't be reached, we fall back to Meta's public Ad
          Library page-typeahead, which maps a name to its page id directly.

  STEP 2  Build the Ad Library URL for THAT page id and scrape every ad:
              https://www.facebook.com/ads/library/?...&view_all_page_id=<ID>
          The grid lazy-loads on scroll, so we scroll until the ad count stops
          growing (full HTML loaded), then parse each ad card straight out of the
          rendered DOM: Library ID, Active/Inactive, start date, ad copy, image/
          video creative URLs, and the click-through link.

Why Playwright: the Ad Library is a JS-rendered React app behind a cookie wall;
a plain HTTP GET returns an empty shell. Playwright drives a real Chromium so the
ads actually render before we read the HTML.

Usage:
    python3 meta_ad_scraper.py vivonu
    HEADFUL=1 python3 meta_ad_scraper.py "vivonu"     # watch it run (debug)

Requires: pip install playwright && python3 -m playwright install chromium
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

import ai_resolve  # narrow AI decision points (website verify, click, extract, disambiguate)

# Shared brand -> official-website-domain resolution (single source of truth for
# both the Meta and Google scrapers). UA/http_get/clean_brand/_dom and the
# website-ranking + AI-discovery helpers all live here so the two can't drift.
from brand_web import (
    UA, http_get, clean_brand, _dom,
    rank_websites, discover_website_ai,
)

# Numeric page-id patterns, most reliable first. We deliberately avoid viewer
# ids like "USER_ID"/"actorID" (those are the logged-out visitor, not the page).
PID_PATTERNS = [
    r'fb://page/\?id=(\d{5,})',           # al:android:url meta tag
    r'fb://profile/(\d{5,})',             # al:ios:url meta tag
    r'"pageID":"(\d{5,})"',
    r'"page_id":"?(\d{5,})"?',
    r'"delegate_page":\{"id":"(\d{5,})"',
    r'"entity_id":"(\d{5,})"',
    r'set:\s*"PAGE",\s*"id":\s*"(\d{5,})"',
]


def handle_variants(brand):
    """Plausible Facebook vanity handles for a brand string."""
    b = brand.strip()
    compact = re.sub(r"[^A-Za-z0-9.]", "", b)
    dotted = re.sub(r"\s+", ".", b.strip())
    out = []
    for h in (compact, compact.lower(), dotted, dotted.lower(), b.replace(" ", "")):
        if h and h not in out:
            out.append(h)
    return out


def extract_pid(html):
    for pat in PID_PATTERNS:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


# ----------------------------------------------------------- STEP 1: page id
#
# The hard part isn't reading a page id — it's reading the RIGHT one. A bare
# name like "vivonu" matches a dozen look-alike/reseller pages in Meta's search
# (VivoNu, Vivonu., @SNAP.SUPLEMENTOS ...). So we resolve in order of CONFIDENCE:
#
#   A. brand -> the brand's OWN website -> the facebook.com link in its footer
#      -> numeric page id. Anything reached from the brand's own domain is
#      unambiguously theirs. This is the route the pipeline already trusts.
#   B. guess the vanity handle (facebook.com/<brand>) and read its page id.
#   C. last resort: the public Ad Library typeahead (ambiguous; we pick the best
#      name match and surface the runners-up so a human can sanity-check).
#
# Facebook serves anonymous/headless visitors a login wall, so for A and B we do
# NOT scrape facebook.com/<page> directly — we use the public page-plugin
# endpoint (plugins/page.php?href=...), which renders the page id for anyone.

# Numeric page-id patterns as they appear in the page-plugin / page HTML.
FB_PAGEID_RES = [
    re.compile(r'"pageID":"(\d+)"'),
    re.compile(r'"page_id":"(\d+)"'),
    re.compile(r'"entity_id":"(\d+)"'),
    re.compile(r'fb://page/\?id=(\d+)'),
    re.compile(r'fb://page/(\d+)'),
]

def is_explicit_profile_url(fb_url=""):
    """True only when the URL itself names a personal PROFILE (not a Page).

    We learned the hard way that the numeric id PREFIX is NOT a reliable
    profile signal: real Facebook Pages routinely carry ids beginning 1000…
    (brooklinen 100064…, native 100063…) and even 100000… The only trustworthy
    "this is a profile" signal from a URL is an explicit profile path. Page-vs-
    profile is otherwise decided by PROVENANCE: an id returned by the public
    page-plugin (plugins/page.php) is, by construction, a Page (the plugin only
    renders Pages); see page_id_from_fb_url(..., want_source=True)."""
    u = (fb_url or "").lower()
    return "profile.php?id=" in u or "/people/" in u


def page_id_from_fb_url(fb_url, want_source=False):
    """Resolve a Facebook page/profile URL to its numeric page id.

    Tries the PUBLIC page-plugin endpoint FIRST (no login), then the raw page
    and mbasic as fallbacks. The caller vouches that `fb_url` belongs to the
    brand, so whatever id we read is confidently the brand's.

    With want_source=True, returns (page_id, from_plugin) where from_plugin is
    True iff the id came from plugins/page.php. That endpoint only renders real
    PAGES, so from_plugin==True is a reliable "this is a Page (not a profile)"
    signal — far more trustworthy than guessing from the id's digit prefix.
    """
    # profile.php?id=<digits> already carries the id — and is an explicit profile.
    m = re.search(r"profile\.php\?id=(\d+)", fb_url)
    if m:
        return (m.group(1), False) if want_source else m.group(1)

    plugin = ("https://www.facebook.com/plugins/page.php?href="
              + urllib.parse.quote(fb_url, safe="")
              + "&tabs=timeline&width=340&height=500")
    candidates = [(plugin, True), (fb_url, False)]
    try:
        path = urllib.parse.urlparse(fb_url).path
        if path and path != "/":
            candidates.append(("https://mbasic.facebook.com" + path, False))
    except Exception:
        pass
    for url, from_plugin in candidates:
        try:
            html = http_get(url, timeout=12)
        except Exception:
            continue
        for rx in FB_PAGEID_RES:
            mm = rx.search(html)
            if mm:
                return (mm.group(1), from_plugin) if want_source else mm.group(1)
    return (None, False) if want_source else None


def facebook_link_on_site(website_url):
    """Fetch the brand's website and return the facebook.com link it advertises
    (usually a footer/social icon). That link is the brand's own page."""
    if not website_url:
        return None
    if not website_url.startswith("http"):
        website_url = "https://" + website_url
    try:
        html = http_get(website_url, timeout=12)
    except Exception:
        return None
    # Collect facebook.com hrefs, skip share/sharer/plugins/tr noise.
    links = re.findall(r'https?://(?:www\.|m\.|web\.)?facebook\.com/[^\s"\'<>)]+', html)
    for ln in links:
        low = ln.lower()
        if any(b in low for b in ("/sharer", "/share.php", "/plugins/", "/tr?",
                                  "/dialog/", "facebook.com/v")):
            continue
        # strip trailing punctuation / query noise
        ln = ln.rstrip('/').split("?")[0]
        if re.search(r"facebook\.com/[^/]+$", ln) or "profile.php" in ln:
            return ln
    return None


# --- in-page readers (code does the looking; AI only judges the result) ---

_FB_LINKS_JS = r"""
() => {
  const ok = h => h && /facebook\.com/.test(h) &&
    !/\/(sharer|share\.php|plugins|dialog|tr\?|v\d)/.test(h) &&
    !/facebook\.com\/(login|help|policies|business|legal)/.test(h);
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) if (ok(a.href)) out.push(a.href);
  return [...new Set(out)];
}
"""

_LINK_DUMP_JS = r"""
() => {
  const hrefs = [...new Set(Array.from(document.querySelectorAll('a[href]'))
      .map(a => a.href).filter(Boolean))].slice(0, 150);
  const html = document.documentElement.innerHTML;
  const extras = [...new Set((html.match(/[^"'<>\s]{0,40}facebook[^"'<>\s]{0,60}/gi) || []))].slice(0, 40);
  const foot = document.querySelector('footer');
  const footer = ((foot ? foot.innerText : document.body.innerText) || '').slice(-800);
  return { hrefs, extras, footer };
}
"""


def _dom_fb_link(page):
    try:
        links = page.evaluate(_FB_LINKS_JS)
    except Exception:
        links = []
    for ln in links:
        ln = ln.rstrip("/").split("?")[0]
        if re.search(r"facebook\.com/[^/]+$", ln) or "profile.php" in ln:
            return ln
    return None


def _clickables(page):
    """Visible buttons/links with short labels -> [(handle, text)]. The AI gets
    only the texts and returns an index; we click that handle."""
    out, seen = [], set()
    try:
        els = page.query_selector_all("button, [role='button'], a")
    except Exception:
        return out
    for h in els:
        try:
            if not h.is_visible():
                continue
            t = (h.inner_text() or "").strip().replace("\n", " ")
        except Exception:
            continue
        if not t or len(t) > 40 or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append((h, t))
        if len(out) >= 40:
            break
    return out


def fb_link_from_rendered_site(page, url, brand):
    """Render the site (JS), step past any interstitial (AI picks the click),
    then read the brand's Facebook link (deterministic, else AI extraction)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        return None
    _dismiss_cookies(page)
    page.wait_for_timeout(2000)

    for _ in range(3):
        fb = _dom_fb_link(page)
        if fb:
            return fb
        els = _clickables(page)
        if not els:
            break
        idx = ai_resolve.choose_click(
            brand, [{"i": i, "text": t} for i, (_h, t) in enumerate(els)])
        if idx is None or idx < 0 or idx >= len(els):
            break
        try:
            els[idx][0].click(timeout=4000)
            page.wait_for_timeout(2500)
        except Exception:
            break

    fb = _dom_fb_link(page)
    if fb:
        return fb
    try:
        dump = page.evaluate(_LINK_DUMP_JS)
    except Exception:
        dump = {}
    return ai_resolve.extract_facebook(
        brand, dump.get("hrefs", []), dump.get("extras", []), dump.get("footer", ""))


def resolve_via_website(brand, products, page):
    """ROUTE A (highest confidence): brand -> own verified website -> its
    facebook link -> page id. Iterates ranked brand domains and returns the
    first that yields a real page id. Returns (page_id, note, website) where
    `website` is the best domain found (for downstream disambiguation), even on
    failure; or (None, note, "")."""
    def try_domain(site):
        """One domain -> (page_id, fb_url) or (None, None)."""
        url = "https://" + site
        fb = facebook_link_on_site(url)              # fast static fetch
        if not fb:
            fb = fb_link_from_rendered_site(page, url, brand)  # JS render + AI
        if not fb:
            return None, None
        return page_id_from_fb_url(fb), fb

    doms, note = rank_websites(brand, products)
    tried_ai = False
    if not doms:
        # deterministic guessing found nothing -> AI searches for the site
        site, dnote = discover_website_ai(brand, products)
        tried_ai = True
        if site:
            doms, note = [site], dnote
    if not doms:
        return None, note, ""

    best = doms[0]
    seen = {_dom(d) for d in doms}
    for site in doms:
        pid, fb = try_domain(site)
        if pid:
            return pid, f"{site} -> {fb} [{note}]", site

    # Deterministic domains exhausted without a page id. The real domain may not
    # be the bare brand name (caraway -> carawayhome.com), so escalate to AI
    # web-discovery before conceding Route A — but only if we haven't already.
    if not tried_ai:
        site, dnote = discover_website_ai(brand, products)
        d = _dom(site) if site else ""
        if d and d not in seen:
            pid, fb = try_domain(d)
            if pid:
                return pid, f"{d} -> {fb} [{dnote}]", d
            best = best or d
    return None, f"no page id from {len(doms)} site(s) [{note}]", best


def resolve_via_vanity(brand):
    """ROUTE B: guess facebook.com/<handle> and read its page id via the public
    plugin endpoint (no login wall).

    Returns (page_id, fb_url, from_plugin). from_plugin==True means the id came
    from the page-plugin, i.e. the guessed vanity resolves to a real PAGE — the
    caller treats that as a confident hit (the handle == brand is the provenance).
    A hit found only via the raw/mbasic fallback (from_plugin==False) is left to
    Route C's product-context judgment. Returns (None, None, False) if nothing."""
    for h in handle_variants(brand):
        fb = "https://www.facebook.com/" + urllib.parse.quote(h)
        pid, from_plugin = page_id_from_fb_url(fb, want_source=True)
        if pid:
            return pid, fb, from_plugin
    return None, None, False


# --- Ad Library typeahead (advertiser rows, parsed as a human sees them) ---

def _parse_advertiser_row(text):
    """Turn a typeahead advertiser row's text into {name, category, followers,
    handle}. Returns None for non-advertiser rows (e.g. keyword suggestions)."""
    t = " ".join((text or "").split())
    if not t or ("follow" not in t.lower() and "@" not in t and "·" not in t):
        return None
    if "search this exact phrase" in t.lower():
        return None
    name = (text or "").strip().split("\n")[0].strip()
    handle = (re.search(r"@([\w.]+)", t) or [None, ""])[1]
    foll = (re.search(r"([\d.,]+[KM]?)\s+follow", t)
            or re.search(r"·\s*([\d.,]+[KM]?)\s+followers", t))
    followers = foll.group(1) if foll else ""
    cat = ""
    for seg in re.findall(r"·\s*([^·@]+)", t):
        s = seg.strip()
        if s and "follow" not in s.lower() and not re.match(r"^[\d.,]+[KM]?$", s):
            cat = s
            break
    return {"name": name, "category": cat, "followers": followers, "handle": handle}


def resolve_via_adlibrary_search(page, brand, products, website):
    """ROUTE C: drive the public Ad Library typeahead, collect the advertiser
    suggestions exactly as a human sees them, and let the meta-page-disambiguate
    skill pick the brand's real page. We click that row and read its
    view_all_page_id. Returns (page_id, name) or (None, None)."""
    url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
           "&country=ALL&media_type=all&search_type=keyword_unordered&q="
           + urllib.parse.quote(brand))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return None, None
    _dismiss_cookies(page)
    page.wait_for_timeout(3000)

    box = None
    for finder in (
        lambda: page.get_by_placeholder(re.compile(r"search by keyword|advertiser", re.I)),
        lambda: page.locator("input[type='search']"),
        lambda: page.locator("input[placeholder*='Search']"),
    ):
        try:
            loc = finder().first
            loc.wait_for(state="visible", timeout=4000)
            box = loc
            break
        except Exception:
            continue
    if box is None:
        return None, None
    try:
        box.click(timeout=3000)
        box.fill("")
        box.type(brand, delay=70)
        page.wait_for_timeout(3000)
    except Exception:
        return None, None

    # collect advertiser suggestion rows (handle + parsed fields)
    rows, seen = [], set()
    try:
        opts = page.query_selector_all("[role='option']")
    except Exception:
        opts = []
    for h in opts:
        try:
            txt = h.inner_text()
        except Exception:
            continue
        rec = _parse_advertiser_row(txt)
        if not rec or not rec["name"] or rec["name"].lower() in seen:
            continue
        seen.add(rec["name"].lower())
        rows.append((h, rec))
        if len(rows) >= 15:
            break
    if not rows:
        return None, None

    if len(rows) == 1:
        chosen = 0
    else:
        cands = [dict(i=i, **rec) for i, (_h, rec) in enumerate(rows)]
        idx, _why = ai_resolve.disambiguate_pages(brand, products, website, cands)
        if idx is None or idx < 0 or idx >= len(rows):
            return None, None
        chosen = idx

    handle, rec = rows[chosen]
    try:
        handle.click(timeout=4000)
        page.wait_for_timeout(3000)
    except Exception:
        return None, None
    m = re.search(r"view_all_page_id=(\d{5,})", page.url)
    if m:
        return m.group(1), rec["name"]
    pid = extract_pid(page.content())
    return (pid, rec["name"]) if pid else (None, None)


# ------------------------------------------------------- STEP 2: scrape ads

ADLIB_URL = ("https://www.facebook.com/ads/library/?active_status=active"
             "&ad_type=all&country=ALL&media_type=all&search_type=page"
             "&view_all_page_id={pid}")

# Runs in the page: group the DOM into ad cards and pull structured fields.
EXTRACT_JS = r"""
() => {
  const txt = el => (el.innerText || '');
  const countID = el => (txt(el).match(/Library ID/g) || []).length;

  // Innermost div that holds exactly one "Library ID" (the id's own wrapper)...
  const inner = Array.from(document.querySelectorAll('div')).filter(d =>
      countID(d) === 1 &&
      !Array.from(d.querySelectorAll(':scope div')).some(c => countID(c) === 1));

  // ...then climb to the OUTERMOST ancestor still holding exactly one id = card.
  const roots = new Set();
  for (const s of inner) {
    let n = s;
    while (n.parentElement && n.parentElement !== document.body &&
           countID(n.parentElement) === 1) n = n.parentElement;
    roots.add(n);
  }

  const parse = (c) => {
    const t = txt(c);
    const lib = (t.match(/Library ID:?\s*(\d+)/) || [])[1] || null;
    const status = /\bActive\b/.test(t) ? 'Active'
                 : (/\bInactive\b/.test(t) ? 'Inactive' : null);
    const started = (t.match(/Started running on ([^\n·•]+)/) || [])[1] || null;
    const platforms = [...new Set(Array.from(c.querySelectorAll('[aria-label]'))
        .map(e => e.getAttribute('aria-label') || '')
        .filter(a => /facebook|instagram|messenger|audience network/i.test(a)))];
    const images = [...new Set(Array.from(c.querySelectorAll('img'))
        .map(i => i.src)
        .filter(s => s && /scontent|fbcdn/.test(s) && !/static|emoji|rsrc\.php/.test(s)))];
    const videos = [...new Set(Array.from(c.querySelectorAll('video'))
        .map(v => v.src).filter(Boolean))];
    const links = [...new Set(Array.from(c.querySelectorAll('a[href]'))
        .map(a => a.href)
        .filter(h => h && !/facebook\.com|fb\.com|^javascript/.test(h)))];
    return { library_id: lib, status, started,
             platforms, images, videos, links,
             text: t.replace(/[ \t]+\n/g, '\n').trim().slice(0, 1500) };
  };

  return Array.from(roots).map(parse).filter(a => a.library_id);
}
"""


def _dismiss_cookies(page):
    for label in ("Allow all cookies", "Decline optional cookies",
                  "Only allow essential cookies"):
        try:
            page.get_by_role("button", name=label).click(timeout=1500)
            return
        except Exception:
            pass


def _result_count(page):
    try:
        body = page.inner_text("body")
    except Exception:
        return None
    m = re.search(r"~?\s*([\d,]+)\s+results?", body, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    if re.search(r"\bno ads\b|isn'?t running ads|0 results", body, re.I):
        return 0
    return None


def scrape_ads(page, page_id, max_scrolls=None):
    if max_scrolls is None:
        max_scrolls = int(os.environ.get("MAX_SCROLLS", "60"))
    page.goto(ADLIB_URL.format(pid=page_id), wait_until="domcontentloaded",
              timeout=60000)
    _dismiss_cookies(page)
    page.wait_for_timeout(3500)

    # Scroll until the number of loaded cards stops growing (full HTML loaded).
    stable = 0
    last = -1
    for _ in range(max_scrolls):
        n = page.evaluate("() => (document.body.innerText.match(/Library ID/g)||[]).length")
        if n == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last = n
        page.mouse.wheel(0, 6000)
        page.wait_for_timeout(1400)

    total = _result_count(page)
    ads = page.evaluate(EXTRACT_JS)
    return total, ads


# -------------------------------------------------------------------- main

def _finalize(page, brand, pid, route, resolve_url, profile_suspect):
    """STEP 2 for an already-resolved page id: scrape the Ad Library and build
    the result dict (shared by the full-resolution path and the fast-path that
    reuses a brand's known fb_page_id)."""
    if profile_suspect:
        print(f"      WARNING: id {pid} is unverified/possibly-not-a-Page — "
              f"'0 ads' from it may be a false negative", file=sys.stderr)
    print(f"[2/2] scraping Ad Library for page id {pid} ...", file=sys.stderr)
    total, ads = scrape_ads(page, pid)
    # IMPORTANT: total is None when Meta's "N results" text couldn't be parsed —
    # that is UNKNOWN, never zero. Only an explicit 0 (or the "no ads"/"0 results"
    # copy) is a real green-prospect signal.
    status = "resolved"
    if profile_suspect:
        status = "resolved_profile_suspect"
    elif total == 0 and len(ads) == 0:
        status = "resolved_no_ads"   # genuine green-prospect signal
    elif total is None and len(ads) == 0:
        status = "resolved_unknown_count"  # do NOT treat as zero
    return {"brand": brand, "page_id": pid, "resolved_via": route,
            "status": status, "profile_suspect": profile_suspect,
            "resolved_url": resolve_url,
            "ad_library_url": ADLIB_URL.format(pid=pid),
            "reported_count": total, "scraped_count": len(ads),
            "ads": ads}


def run(brand, headless=True, page_id=None):
    """Resolve the brand's Facebook page id and scrape its ads.

    When `page_id` is supplied (e.g. the brands table already resolved it during
    enrichment), STEP 1 is skipped entirely and we scrape that id directly — the
    fast-path the dashboard's 'Process Meta ads' job uses."""
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
            if page_id:
                print(f"[1/2] using provided page id {page_id} for '{brand}' "
                      "(skipping resolution)", file=sys.stderr)
                return _finalize(page, brand, page_id, "preresolved", "", False)
            print(f"[1/2] resolving Facebook page id for '{brand}' ...", file=sys.stderr)
            products = ai_resolve.product_titles(brand)  # niche context for the AI steps
            if products:
                print(f"      product context: {len(products)} title(s) from DB",
                      file=sys.stderr)
            qbrand = clean_brand(brand)  # strip 'LLC' etc. for searching
            if qbrand != brand.strip():
                print(f"      search name: '{qbrand}' (legal suffix stripped)",
                      file=sys.stderr)
            route = None
            resolve_url = ""  # the fb URL the id came from (for the profile guard)
            weak = None       # (pid, url) low-confidence vanity hit, used only if
                              #            the better-judged Route C also fails
            # ROUTE A: brand -> own (AI-verified/discovered) website -> fb -> id.
            pid, note, website = resolve_via_website(qbrand, products, page)
            if pid:
                route = "website"
                resolve_url = note  # note embeds "... -> <fb_url> [...]"
                print(f"      page id {pid}  (via website: {note})", file=sys.stderr)
            # ROUTE B: vanity handle guess. We accept it as final when the guess
            # resolves to a real PAGE via the page-plugin (from_plugin) and the
            # URL isn't an explicit profile — the handle==brand is the provenance.
            # A hit found only via the raw/mbasic fallback is unconfirmed: hold it
            # as a weak fallback and let Route C's product-context judgment try.
            if not pid:
                print(f"      route A failed ({note}); trying vanity handle ...",
                      file=sys.stderr)
                vpid, vurl, vplugin = resolve_via_vanity(qbrand)
                if vpid:
                    if vplugin and not is_explicit_profile_url(vurl):
                        pid, route, resolve_url = vpid, "vanity", vurl
                        print(f"      page id {pid}  (vanity, plugin-confirmed Page)",
                              file=sys.stderr)
                    else:
                        weak = (vpid, vurl)
                        why = "explicit profile" if is_explicit_profile_url(vurl) else \
                              "not plugin-confirmed"
                        print(f"      vanity hit {vpid} held as weak fallback "
                              f"({why}); trying Ad Library ...", file=sys.stderr)
            # ROUTE C: AI-disambiguated Ad Library typeahead (uses the website,
            # if we found one, as an extra disambiguation signal).
            if not pid:
                if weak is None:
                    print("      vanity failed; trying Ad Library typeahead ...",
                          file=sys.stderr)
                pid, name = resolve_via_adlibrary_search(page, qbrand, products, website)
                if pid:
                    route = "ad-library"
                    print(f"      page id {pid}  (Ad Library match: {name or '?'})",
                          file=sys.stderr)
            # Last resort: fall back to the unverified vanity hit (still flagged
            # downstream by the profile guard) rather than reporting nothing.
            if not pid and weak is not None:
                pid, resolve_url = weak
                route = "vanity-unverified"
                print(f"      no better page found; using weak vanity hit {pid}",
                      file=sys.stderr)
            if not pid:
                # No id at all. This is a *resolution gap*, NOT proof of zero ads.
                # Downstream must NOT read this as "0 ads = green prospect".
                return {"brand": brand, "page_id": None, "status": "unresolved",
                        "error": "could not resolve a Facebook page id"}

            # Low-confidence guard. We only doubt the id when (a) the URL is an
            # explicit personal profile (Ad Library shows no profile ads), or
            # (b) it's the unconfirmed vanity fallback. The website/ad-library
            # routes carry their own provenance and are trusted; the numeric id
            # prefix is deliberately NOT used (real Pages start 1000…/61… too).
            profile_suspect = (is_explicit_profile_url(resolve_url)
                               or route == "vanity-unverified")
            return _finalize(page, brand, pid, route, resolve_url, profile_suspect)
        finally:
            browser.close()


def log_resolution(result):
    """Append one compact line per run to data/resolutions.jsonl — the durable,
    queryable record of every resolution (how many handled / success rate /
    which route). The big per-case ads live in data/<brand>_meta_ads.json; this
    is the index over all runs."""
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "brand": result.get("brand"),
        "page_id": result.get("page_id"),
        "resolved_via": result.get("resolved_via"),
        "status": result.get("status"),
        "profile_suspect": result.get("profile_suspect", False),
        "reported_count": result.get("reported_count"),
        "scraped_count": result.get("scraped_count"),
        "resolved_url": result.get("resolved_url", ""),
        "error": result.get("error"),
    }
    try:
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", "resolutions.jsonl"), "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"      (could not append resolutions.jsonl: {e})", file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        print("usage: python3 meta_ad_scraper.py <brand>", file=sys.stderr)
        sys.exit(1)
    brand = " ".join(sys.argv[1:])
    result = run(brand, headless=os.environ.get("HEADFUL") != "1")

    os.makedirs("data", exist_ok=True)
    out = os.path.join("data", re.sub(r"\W+", "_", brand.lower()) + "_meta_ads.json")
    with open(out, "w") as fh:
        json.dump(result, fh, indent=2)
    log_resolution(result)

    print("\n=== RESULT ===")
    print(f"brand          : {result['brand']}")
    print(f"page id        : {result.get('page_id')}")
    print(f"status         : {result.get('status')}")
    if result.get("page_id"):
        if result.get("profile_suspect"):
            print("  !! id looks like a personal PROFILE — '0 ads' may be a false negative")
        print(f"resolved via   : {result.get('resolved_via')}")
        print(f"ad library url : {result['ad_library_url']}")
        print(f"reported count : {result.get('reported_count')}")
        print(f"scraped ads    : {result.get('scraped_count')}")
        for a in result.get("ads", [])[:5]:
            print(f"  - {a['library_id']}  {a.get('status') or ''}  "
                  f"{a.get('started') or ''}  imgs={len(a['images'])} vids={len(a['videos'])}")
        if result.get("scraped_count", 0) > 5:
            print(f"  ... +{result['scraped_count'] - 5} more")
    else:
        print(f"error          : {result.get('error')}")
    print(f"\nfull JSON -> {out}")


if __name__ == "__main__":
    main()
