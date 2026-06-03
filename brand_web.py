#!/usr/bin/env python3
"""Shared brand -> official-website-domain resolution.

Both ad scrapers (meta_ad_scraper.py, google_ad_scraper.py) need the same first
move: turn an Amazon brand string into the brand's OWN web domain. Meta then
reads the Facebook link off that site; Google keys its Ad Transparency Center
directly on the domain. This module is the single source of truth for that step
so the two scrapers can't drift apart.

Design (same contract as the rest of the project):
  * Code does the looking (deterministic web probes via websearch); the model
    only JUDGES which candidate domain is really the brand's, and ABSTAINS when
    unsure. We fall back to the deterministic order so the happy path never
    depends on the AI being available.
"""

import re
import urllib.parse
import urllib.request

try:
    import websearch  # brand -> candidate website(s)
except Exception:                       # pragma: no cover
    websearch = None

import ai_resolve  # narrow AI decision points (verify_website lives here)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


# Legal-entity suffixes that poison domain/handle/search guesses: an Amazon
# seller "NutroTonic LLC" sells at nutrotonic.com, not nutrotonicllc.com.
LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(llc|l\.l\.c\.?|inc\.?|incorporated|ltd\.?|limited|corp\.?|"
    r"co\.?|company|gmbh|llp|lp|plc|pty|pvt|ag|sa|srl|bv|oy|ab)\.?$", re.I)


def clean_brand(brand):
    """Strip trailing legal suffixes for SEARCHING (website/handle/typeahead).
    The original brand is still used for the DB product lookup."""
    b = (brand or "").strip()
    prev = None
    while prev != b:
        prev = b
        b = LEGAL_SUFFIX_RE.sub("", b).strip().rstrip(",").strip()
    return b or (brand or "").strip()


def _dom(url):
    d = urllib.parse.urlparse(url if "://" in url else "http://" + url).netloc
    d = (d or url).lower()
    # strip the literal "www." prefix only (lstrip("www.") would eat leading
    # w/. characters from real domains, e.g. weruva.com -> eruva.com).
    if d.startswith("www."):
        d = d[4:]
    return d


def rank_websites(brand, products):
    """Return brand-site domains to try, best first, plus a note.

    Deterministic candidates (probes whose domain core == the brand) come from
    websearch. When more than one is plausible we ask the meta-website-verify
    skill to name the real one (noon.com vs noon.ae) using the product context,
    and float that to the front. If the skill abstains or is unavailable, we
    still return the deterministic order so the happy path keeps working.
    """
    if websearch is None:
        return [], "no websearch"
    try:
        cands = websearch.find_websites(brand, max_n=4)
    except Exception:
        cands = []
    if not cands:
        return [], "no website candidates"
    doms, seen = [], set()
    for c in cands:
        d = _dom(c["url"])
        if d and d not in seen:
            seen.add(d)
            doms.append(d)
    note = "deterministic order"
    if len(doms) > 1:
        rows = [{"domain": _dom(c["url"]), "title": c.get("title", ""),
                 "snippet": c.get("source", "")} for c in cands]
        site, why = ai_resolve.verify_website(brand, products, rows)
        if site and _dom(site) in seen:
            d = _dom(site)
            doms = [d] + [x for x in doms if x != d]
            note = f"AI-verified ({why})"
    return doms, note


def discover_website_ai(brand, products):
    """When deterministic guessing finds NOTHING (e.g. the domain isn't the bare
    brand name), let the AI search the web for the brand's official site, using
    a product as context. This is the 'AI crafts the query' escalation. Returns
    (domain, note) or (None, note)."""
    if websearch is None or not hasattr(websearch, "ai_find_website"):
        return None, "no AI discovery"
    try:
        r = websearch.ai_find_website(brand, context=(products[0] if products else ""))
    except Exception as e:
        return None, f"AI discovery error: {e}"
    site = (r or {}).get("website_url")
    if site:
        return _dom(site), f"AI-discovered ({(r.get('website_remarks') or '')[:60]})"
    return None, "AI found no site"


def brand_domains(brand, products):
    """Best-first list of the brand's own web domains, plus a note.

    Combines the deterministic ranked probes with the AI web-discovery fallback
    so a single call gives callers every domain worth trying. Returns
    (domains, note); domains is [] when nothing plausible was found."""
    doms, note = rank_websites(brand, products)
    if doms:
        return doms, note
    site, dnote = discover_website_ai(brand, products)
    if site:
        return [site], dnote
    return [], note
