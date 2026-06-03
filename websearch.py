"""Robust website discovery for a brand name.

Why this exists: search-engine scraping alone (DuckDuckGo) was unreliable and
rate-limited, so distinctive brands like "VivoNu" came back with NO website even
though vivonu.com plainly exists. A person wouldn't give up there — they'd just
type vivonu.com. This module does the same, deterministically:

  1. DIRECT PROBE (primary): build <brand>.<tld> guesses for common TLDs, fetch
     each in parallel, accept the ones that resolve to a live (non-parked) site.
     Catches the common case with zero dependence on any search engine.
  2. GOOGLE (fill): query the exact brand, read the first ~20 organic results,
     and add up to `max_n` domains whose hostname CONTAINS the brand name. This
     catches sites whose domain isn't the bare brand (e.g. <brand>nutrition.com).
  3. Merge: exact bare-name domains first (high confidence), then contains.

Returns MULTIPLE candidates (the dashboard renders them as clickable links the
human verifies). `ai_find_website` is the on-demand escalation: it shells out to
the Claude CLI (your subscription, not the API) to pin the site precisely and
return a short remark explaining how it was confirmed.
"""

import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

import search

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# TLDs tried for the bare-name probe, in priority order (.com first).
TLDS = ("com", "co", "us", "eu", "net", "shop", "store", "io")

BLOCKLIST = (
    "amazon.", "amzn.", "walmart.", "ebay.", "iherb.", "facebook.", "instagram.",
    "youtube.", "youtu.be", "pinterest.", "tiktok.", "twitter.", "x.com", "reddit.",
    "wikipedia.", "target.", "costco.", "gnc.", "vitaminshoppe.", "etsy.", "alibaba.",
    "aliexpress.", "shopee.", "flipkart.", "google.", "bing.", "yelp.", "linkedin.",
    "trustpilot.", "indeed.", "glassdoor.", "yahoo.", "msn.", "quora.", "medium.",
    "wordpress.", "blogspot.", "apps.apple.com", "play.google.com",
)
REJECT_SUBSTR = ("news", "blog", "review", "wiki", "forum", "coupon", "deal", "magazine")
PARKED = (
    "domain is for sale", "buy this domain", "this domain may be for sale",
    "domain parking", "is parked", "hugedomains", "sedoparking", "parkingcrew",
    "bodis.com", "afternic", "godaddy.com/domainsearch", "dan.com",
)
GOOGLE_SKIP = ("google.", "gstatic.", "googleusercontent.", "youtube.", "webcache",
               "schema.org", "w3.org", "googleadservices")


def compact(s):
    return search.normalize(s).replace(" ", "")


def domain_of(url):
    try:
        net = urllib.parse.urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def is_blocked(d):
    return any(b in d for b in BLOCKLIST) or any(s in d for s in REJECT_SUBSTR)


def http_get(url, timeout=8, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(200000).decode("utf-8", "ignore")


def _title(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def probe(domain, core):
    """Fetch a guessed domain; return a candidate dict if it's a live real site."""
    for scheme in ("https://", "http://"):
        try:
            html = http_get(scheme + domain, timeout=7)
        except Exception:
            continue
        low = html.lower()
        if any(p in low for p in PARKED):
            return None
        title = _title(html)
        stem = domain.split(".")[0].replace("-", "")
        conf = "high" if (stem == core or core in compact(title)) else "review"
        return {"url": "https://" + domain, "title": title,
                "confidence": conf, "source": "probe"}
    return None


def guess_domains(brand):
    core = compact(brand)
    if len(core) < 3:
        return []
    doms = [f"{core}.{t}" for t in TLDS]
    words = search.normalize(brand).split()
    if len(words) > 1:
        hy = "-".join(words)
        doms += [f"{hy}.com", f"{hy}.co"]
    return doms


def google_results(query, n=20):
    """Best-effort parse of Google organic result domains -> [(domain, url)]."""
    url = "https://www.google.com/search?hl=en&num=20&q=" + urllib.parse.quote(query)
    cookie = "CONSENT=YES+cb.20210720-07-p0.en+FX+410"
    try:
        html = http_get(url, timeout=10,
                        headers={"Cookie": cookie, "Accept-Language": "en-US,en;q=0.9"})
    except Exception:
        return []
    raw = []
    for m in re.finditer(r"/url\?q=(https?://[^&\"]+)", html):
        raw.append(urllib.parse.unquote(m.group(1)))
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        raw.append(m.group(1))
    out, seen = [], set()
    for u in raw:
        d = domain_of(u)
        if not d or d in seen or any(s in d for s in GOOGLE_SKIP):
            continue
        seen.add(d)
        out.append((d, u))
        if len(out) >= n:
            break
    return out


def is_ambiguous(brand):
    """Short single-token names (e.g. 'VEV') collide with unrelated companies, so
    a bare-domain hit isn't proof. Such matches stay 'review', never 'high'."""
    toks = search.normalize(brand).split()
    return len(toks) == 1 and len(toks[0]) <= 4


def find_websites(brand, context="", max_n=5):
    """Return up to max_n candidate dicts: {url, title, confidence, source}."""
    core = compact(brand)
    if not core:
        return []
    amb = is_ambiguous(brand)
    cands, seen = [], set()

    # 1) direct probe in parallel
    doms = guess_domains(brand)
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda d: probe(d, core), doms):
            if not r:
                continue
            d = domain_of(r["url"])
            if d not in seen and not is_blocked(d):
                seen.add(d)
                cands.append(r)

    # 2) google fill (domains containing the brand name)
    if len(cands) < max_n:
        for d, _u in google_results(brand):
            if d in seen or is_blocked(d):
                continue
            flat = d.replace(".", "").replace("-", "")
            if core in flat:
                seen.add(d)
                conf = "high" if d.split(".")[0].replace("-", "") == core else "review"
                cands.append({"url": "https://" + d, "title": "",
                              "confidence": conf, "source": "google"})
            if len(cands) >= max_n:
                break

    if amb:
        for c in cands:
            c["confidence"] = "review"
    order = {"probe": 0, "google": 1}
    cands.sort(key=lambda c: (0 if c["confidence"] == "high" else 1,
                              order.get(c["source"], 2)))
    return cands[:max_n]


# ----------------------------------------------------------------- AI escalation

def find_claude():
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    default = os.path.expanduser("~/.local/bin/claude")
    return default if os.path.exists(default) else None


def ai_find_website(brand, context=""):
    """On-demand: ask the Claude CLI to pin the official site + a short remark.
    Returns {website_url, website_urls, website_remarks, confidence}."""
    b = find_claude()
    if not b:
        return {"website_url": "", "website_urls": [],
                "website_remarks": "Claude CLI not found", "confidence": "none"}
    ctx = f' One product they sell on Amazon: "{context}".' if context else ""
    prompt = (
        f'Find the official direct-to-consumer website for the Amazon brand "{brand}".{ctx} '
        "Use web search and verify the site actually sells THIS brand, not a different "
        "company with a similar name. "
        "Respond with ONLY one line of compact JSON and nothing else: "
        '{"website":"rootdomain.com","remarks":"how you confirmed it and your confidence"}. '
        'If you cannot confirm one, respond {"website":"UNKNOWN","remarks":"why"}.'
    )
    try:
        out = subprocess.run([b, "-p", prompt], capture_output=True, text=True,
                             timeout=200).stdout.strip()
    except Exception as e:
        return {"website_url": "", "website_urls": [],
                "website_remarks": f"AI error: {e}", "confidence": "none"}
    data = {}
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = {}
    site = (data.get("website") or "").strip()
    remarks = (data.get("remarks") or out[:200] or "").strip()
    if not site or site.upper() == "UNKNOWN":
        return {"website_url": "", "website_urls": [],
                "website_remarks": remarks or "AI could not confirm a site",
                "confidence": "none"}
    dom = domain_of("http://" + site.replace("https://", "").replace("http://", "")) or site.lower()
    if is_blocked(dom) or "." not in dom:
        return {"website_url": "", "website_urls": [],
                "website_remarks": remarks or "AI returned an invalid domain",
                "confidence": "none"}
    return {"website_url": dom, "website_urls": ["https://" + dom],
            "website_remarks": remarks, "confidence": "ai"}
