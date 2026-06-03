"""Best-effort ad-count scraping for Meta + Google ad libraries.

HONEST LIMITS. Both libraries are JavaScript-rendered and actively
bot-protected, so counts require a headless browser (Playwright). Every function
returns an int when confident or None when it cannot get a trustworthy number —
None means "unknown", never a guess. The pipeline treats None as "not scraped".

Enable:
  pip install playwright && python3 -m playwright install chromium
"""

import re

try:
    from playwright.sync_api import sync_playwright
    AVAILABLE = True
except Exception:
    AVAILABLE = False

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _new_page(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US")
    return browser, ctx, ctx.new_page()


def meta_ad_count(page_id):
    """Active ads for a specific Facebook page (their OWN ads)."""
    if not AVAILABLE or not page_id:
        return None
    url = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
           f"&country=ALL&view_all_page_id={page_id}&media_type=all")
    try:
        with sync_playwright() as p:
            browser, ctx, page = _new_page(p)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)
                # dismiss cookie dialog if present
                for label in ("Allow all cookies", "Decline optional cookies", "Only allow essential cookies"):
                    try:
                        page.get_by_role("button", name=label).click(timeout=1500)
                        break
                    except Exception:
                        pass
                page.wait_for_timeout(2000)
                text = page.inner_text("body")
            finally:
                browser.close()
        m = re.search(r"~?\s*([\d,]+)\s+results", text)
        if m:
            return int(m.group(1).replace(",", ""))
        if re.search(r"\bno ads\b|0 results|isn'?t running ads", text, re.I):
            return 0
        return None
    except Exception:
        return None


def google_ad_count(domain):
    """Approximate count of creatives shown for an advertiser domain.
    Google's Transparency Center has no total-count label, so we count rendered
    creative cards after lazy-load. Returns a lower-bound int, or None."""
    if not AVAILABLE or not domain:
        return None
    url = f"https://adstransparency.google.com/?region=anywhere&domain={domain}"
    try:
        with sync_playwright() as p:
            browser, ctx, page = _new_page(p)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)
                for _ in range(6):  # lazy-load
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1200)
                cards = 0
                for sel in ("creative-preview", "priority-creative-grid creative-preview",
                            "[role='listitem']"):
                    try:
                        c = len(page.query_selector_all(sel))
                        cards = max(cards, c)
                    except Exception:
                        pass
                body = page.inner_text("body")
            finally:
                browser.close()
        if cards > 0:
            return cards
        if re.search(r"no ads|couldn'?t find|0 results", body, re.I):
            return 0
        return None
    except Exception:
        return None
