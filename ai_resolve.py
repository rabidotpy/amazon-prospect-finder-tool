#!/usr/bin/env python3
"""Narrow AI decision points for brand -> Facebook page-id resolution.

Design contract (see the matching SKILL.md files in ~/.claude/skills/):

  * Code does ALL the looking — Playwright drives the browser, HTTP fetches the
    HTML, SQL pulls the product context. The model never operates a browser.
  * The model is invoked ONLY at the few moments where code is blind and human
    judgment is required, and each call is *small text in -> compact JSON out*:
        1. verify_website     — is this candidate domain really the brand's site?
        2. choose_click       — which element passes the country/age/cookie gate?
        3. extract_facebook   — find the FB link a regex scan missed.
        4. disambiguate_pages — pick the real page among Ad Library look-alikes.
  * Each decision is governed by its own SKILL.md (the single source of truth for
    the procedure + JSON contract). We read that file and inline it in the prompt
    so behaviour is identical every run and we depend on no auto-discovery.
  * The model ABSTAINS (returns UNKNOWN/null) when unsure — we never guess a page.

These run via the local `claude` CLI (same hook websearch.py already uses), so
there is no API key to manage. Calls are rare (only when deterministic routes
fail) and tiny, keeping token spend near zero.
"""

import json
import os
import re
import shutil
import subprocess

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
_SKILL_CACHE = {}


def claude_bin():
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    default = os.path.expanduser("~/.local/bin/claude")
    return default if os.path.exists(default) else None


def _real_api_key():
    """A real Anthropic API key from secrets.json, if the user added one."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "secrets.json")) as fh:
            d = json.load(fh)
        return (d.get("ANTHROPIC_API_KEY") or d.get("anthropic_api_key") or "").strip() or None
    except Exception:
        return None


def _claude_env():
    """Environment for the `claude` CLI subprocess.

    A harness may inject ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL pointing at an
    internal gateway; those OVERRIDE the CLI's own OAuth login and cause 401s. So:
      • if the user supplied a real key in secrets.json -> use it (and clear the
        proxy base url), or
      • otherwise strip both injected vars so the CLI falls back to your OAuth
        login (claude /login)."""
    env = dict(os.environ)
    key = _real_api_key()
    if key:
        env["ANTHROPIC_API_KEY"] = key
        env.pop("ANTHROPIC_BASE_URL", None)
    else:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
    return env


def available(skill="meta-website-verify"):
    """True if the local claude CLI and the given skill body are both present, so
    AI escalation will actually run (vs silently abstaining)."""
    return bool(claude_bin()) and bool(_skill_body(skill))


def _skill_body(name):
    """Return the SKILL.md body (YAML frontmatter stripped)."""
    if name in _SKILL_CACHE:
        return _SKILL_CACHE[name]
    path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    try:
        with open(path) as fh:
            text = fh.read()
    except Exception:
        text = ""
    # strip a leading ---\n...\n--- frontmatter block
    m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.S)
    if m:
        text = text[m.end():]
    _SKILL_CACHE[name] = text.strip()
    return _SKILL_CACHE[name]


def _ask(skill, payload, timeout=120):
    """Run one narrow skill: inline its SKILL.md + the JSON payload, get JSON back.
    Returns the parsed dict, or **None** when the call could not be made / produced
    no parseable JSON (CLI missing, auth/401 error, timeout, garbage). The None vs
    dict distinction lets callers tell 'AI failed' apart from 'AI said unknown' —
    important so a failed call never masquerades as a confident abstention."""
    b = claude_bin()
    body = _skill_body(skill)
    if not b or not body:
        return None
    prompt = (
        body
        + "\n\n---\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n\nReason from the rules above. Do not use any tools or web search. "
          "Output ONLY the single line of JSON specified — no prose, no code fence."
    )
    try:
        out = subprocess.run([b, "-p", prompt], capture_output=True, text=True,
                             timeout=timeout, env=_claude_env()).stdout.strip()
    except Exception:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ----------------------------------------------------------------- decisions

# Sentinel returned as the `why` when the AI call could not be made (CLI missing /
# 401 / timeout). Callers that need to tell 'AI failed' from 'AI said no site'
# compare against this; plain callers just see (None, <sentinel>) and abstain.
AI_UNAVAILABLE = "__ai_unavailable__"


def ai_failed(why):
    """True if a (site, why) result means the AI call itself failed (vs abstained)."""
    return why == AI_UNAVAILABLE


def verify_website(brand, products, candidates):
    """candidates: [{"domain","title","snippet"}]. Returns (domain, why) or
    (None, why). When the AI call fails, why == AI_UNAVAILABLE so callers can
    distinguish an outage from a confident 'no site'."""
    if not candidates:
        return None, "no candidates"
    data = _ask("meta-website-verify", {
        "brand": brand,
        "products": products[:15],
        "candidates": candidates[:6],
    })
    if data is None:
        return None, AI_UNAVAILABLE
    site = (data.get("website") or "").strip()
    why = (data.get("why") or "").strip()
    if not site or site.upper() == "UNKNOWN" or data.get("confidence") == "none":
        return None, why or "unconfirmed"
    return site, why


def choose_click(brand, elements):
    """elements: [{"i","text"}]. Returns index to click, or None if no gate."""
    if not elements:
        return None
    data = _ask("meta-interstitial-click", {"brand": brand, "elements": elements[:40]}) or {}
    c = data.get("click")
    return c if isinstance(c, int) else None


def extract_facebook(brand, hrefs, extras, footer):
    """Returns a facebook.com URL string, or None."""
    if not hrefs and not extras:
        return None
    data = _ask("meta-extract-facebook", {
        "brand": brand,
        "hrefs": hrefs[:150],
        "extras": extras[:40],
        "footer": (footer or "")[:800],
    }) or {}
    fb = (data.get("facebook") or "").strip()
    if not fb or fb.upper() == "UNKNOWN" or "facebook.com" not in fb:
        return None
    return fb


def disambiguate_pages(brand, products, website, candidates):
    """candidates: [{"i","name","category","followers","handle"}].
    Returns (chosen_index, why) or (None, why)."""
    if not candidates:
        return None, "no candidates"
    data = _ask("meta-page-disambiguate", {
        "brand": brand,
        "products": products[:15],
        "website": website or "",
        "candidates": candidates[:15],
    }) or {}
    choice = data.get("choice")
    why = (data.get("why") or "").strip()
    if not isinstance(choice, int) or data.get("confidence") == "none":
        return None, why or "unconfirmed"
    return choice, why


def disambiguate_advertisers(brand, products, domain, candidates):
    """Google Ads Transparency Center analogue of disambiguate_pages.

    candidates: [{"i","advertiserId","name","region","domains":[...]}].
    Returns (chosen_index, why) or (None, why). Abstains (None) when unsure —
    a wrong advertiser is worse than none."""
    if not candidates:
        return None, "no candidates"
    data = _ask("google-advertiser-disambiguate", {
        "brand": brand,
        "products": products[:15],
        "domain": domain or "",
        "candidates": candidates[:15],
    }) or {}
    choice = data.get("choice")
    why = (data.get("why") or "").strip()
    if not isinstance(choice, int) or data.get("confidence") == "none":
        return None, why or "unconfirmed"
    return choice, why


# ----------------------------------------------------------------- DB context

def product_titles(brand, limit=15):
    """Product titles this brand sells, for niche context. Best-effort; [] if
    the DB/products aren't available (e.g. scraping a brand not yet ingested)."""
    try:
        import sqlite3
        import db
        key = db.norm_key(brand)
        conn = sqlite3.connect(db.DB_PATH)
        rows = conn.execute(
            "SELECT DISTINCT title FROM products WHERE brand_key=? "
            "AND title IS NOT NULL AND title<>'' LIMIT ?",
            (key, limit)).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []
