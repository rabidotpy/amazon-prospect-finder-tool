#!/usr/bin/env python3
"""
Local website resolver for the Amazon Prospect Qualifier extension.

Contract: given a brand name, return its official website domain ONLY when
confident. When not confident, return null. Never guess — a wrong domain is a
real error; "unknown" is not.

Order of attempts (deterministic first, AI last):
  1. DuckDuckGo HTML search -> first non-marketplace result whose domain
     actually contains a brand token (otherwise discarded).
  2. Claude Code (`claude -p`) as a fallback, instructed to answer UNKNOWN
     unless highly confident. Uses your Claude Code desktop install — no API key.

Run:  python3 resolver.py          (listens on http://localhost:8765)
"""

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765

# Automated AI fallback via the Claude Code CLI. Uses your Claude subscription,
# NOT the API. Used only when the deterministic search returns nothing.
# Resolution order: $CLAUDE_BIN -> PATH -> the native installer's default path
# (~/.local/bin/claude), since that dir isn't always on a non-login shell PATH.
def _find_claude():
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    default = os.path.expanduser("~/.local/bin/claude")
    return default if os.path.exists(default) else None


CLAUDE_BIN = _find_claude()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Domains that are never a brand's own DTC site.
BLOCKLIST = (
    "amazon.", "amzn.", "walmart.", "ebay.", "iherb.", "facebook.", "instagram.",
    "youtube.", "youtu.be", "pinterest.", "tiktok.", "twitter.", "x.com", "reddit.",
    "wikipedia.", "target.", "costco.", "gnc.", "vitaminshoppe.", "etsy.", "alibaba.",
    "aliexpress.", "shopee.", "flipkart.", "google.", "bing.", "yelp.", "linkedin.",
    "trustpilot.", "indeed.", "glassdoor.", "yahoo.", "msn.", "quora.", "medium.",
    "wordpress.", "blogspot.", "apps.apple.com", "play.google.com",
)

STOPWORDS = {
    "the", "and", "inc", "llc", "ltd", "co", "company", "supplement", "supplements",
    "nutrition", "health", "vitamins", "vitamin", "brand", "official", "store",
    "usa", "for", "with", "plus", "pro", "max", "natural", "labs", "lab",
    # generic marketing words: a brand made only of these can't be matched
    # by domain alone, so we abstain rather than risk a wrong site.
    "premium", "daily", "gold", "advanced", "complete", "complex", "formula",
    "extra", "strength", "support", "care", "life", "wellness", "organic",
    "pure", "prime", "essential", "essentials", "ultra", "best", "super",
    "original", "classic", "active", "clean", "true", "good", "better",
    "power", "energy", "boost", "daily", "plant", "based", "raw", "whole",
}

# Aggregator / non-brand domains: never a brand's own DTC site.
REJECT_SUBSTR = ("news", "blog", "review", "wiki", "forum", "coupon", "deal", "magazine")


def brand_tokens(brand):
    toks = re.findall(r"[a-z0-9]+", brand.lower())
    return [t for t in toks if len(t) >= 3 and t not in STOPWORDS]


def domain_of(url):
    try:
        net = urllib.parse.urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def is_blocked(domain):
    return any(b in domain for b in BLOCKLIST) or any(s in domain for s in REJECT_SUBSTR)


def domain_matches_brand(domain, tokens):
    """Confident match = a real brand token appears in the registrable domain."""
    flat = domain.split(":")[0].replace("-", "").replace(".", "")
    return any(t in flat for t in tokens)


def ddg_search(brand):
    query = f"{brand} official website"
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
    except Exception:
        return None
    # DDG wraps results as /l/?uddg=<encoded-real-url>
    encoded = re.findall(r"uddg=([^&\"']+)", html)
    tokens = brand_tokens(brand)
    if not tokens:
        return None
    for enc in encoded:
        real = urllib.parse.unquote(enc)
        dom = domain_of(real)
        if not dom or is_blocked(dom):
            continue
        if domain_matches_brand(dom, tokens):
            return dom  # confident: matches a brand token, not a marketplace
    return None


def claude_lookup(brand):
    if not CLAUDE_BIN:
        return None
    prompt = (
        "You are verifying a consumer brand's official website. "
        f"Brand name: \"{brand}\" (a product/supplement brand sold on Amazon). "
        "Output ONLY the root domain of their official direct-to-consumer website "
        "(for example: example.com) with no scheme, path, or other text. "
        "If you are not highly confident it is correct, output exactly: UNKNOWN. "
        "Do not guess."
    )
    try:
        out = subprocess.run(
            [CLAUDE_BIN, "-p", prompt],
            capture_output=True, text=True, timeout=90,
        ).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    first = out.splitlines()[0].strip().strip("`").strip()
    if first.upper() == "UNKNOWN":
        return None
    dom = domain_of("http://" + first) or first.lower()
    if not dom or is_blocked(dom):
        return None
    if "." not in dom or " " in dom:
        return None
    # final guard: the AI answer must still relate to the brand
    if not domain_matches_brand(dom, brand_tokens(brand)):
        return None
    return dom


def resolve(brand):
    dom = ddg_search(brand)
    if dom:
        return {"website": dom, "source": "search"}
    dom = claude_lookup(brand)
    if dom:
        return {"website": dom, "source": "claude"}
    return {"website": None, "source": "unknown"}


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        brand = (payload.get("brand") or "").strip()
        result = resolve(brand) if brand else {"website": None, "source": "no-brand"}
        body = json.dumps(result).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"Resolver listening on http://localhost:{PORT}  (Ctrl+C to stop)")
    print("Automated Claude fallback:", CLAUDE_BIN if CLAUDE_BIN else "OFF (unknowns handed to Claude Code in-chat)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
