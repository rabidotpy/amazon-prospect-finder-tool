# Meta Ad Resolver — Project Log & Context

> **Read this first at the start of every session.** It is the durable memory for
> the brand → Facebook page-id → Meta ads work. Update the tables below whenever
> something is finished, decided, or discovered. The structured per-run log lives
> at `data/resolutions.jsonl` (one line per scraper run).

## Goal

Given ONLY an Amazon brand string (e.g. `vivonu`, `NutroTonic LLC`), reliably and
cheaply find that brand's **own** Facebook page id, then scrape every ad that page
runs in the Meta Ad Library. Accuracy matters more than coverage — a **wrong**
page is worse than no page. Used by the prospecting tool: a brand with no website
and < 30 ads is a "green" prospect.

## How we got here (arc)

1. Started by scraping Meta Ad Library ads for a known page id — `scrape_ads()` +
   `EXTRACT_JS` work (validated on gymshark: 3,800 reported, 720 cards parsed).
2. Hard part is Step 1: resolving the **right** page id from just a name. Name
   search is ambiguous (8 look-alike "vivonu" pages). Solved with a confidence
   cascade + narrow AI judgment, NOT open-ended browsing.
3. Principle locked in: **code drives the browser (Playwright); AI only judges**
   reduced text and returns tiny JSON. Reuse the local `claude -p` CLI hook (no
   API key). AI abstains when unsure.

## Architecture

**Routes (Step 1, in confidence order)** — `meta_ad_scraper.py`:
- **A. website** → brand → (AI-verified/-discovered) official site → facebook link
  on the site → page id via public `plugins/page.php` endpoint. Highest trust.
- **B. vanity** → guess `facebook.com/<handle>` → page id via plugin endpoint.
- **C. ad-library** → drive Ad Library typeahead, collect advertiser candidates
  (name/category/followers/handle), AI picks the brand's page, click → read
  `view_all_page_id`.

**Four narrow AI skills** (`~/.claude/skills/`), driven by `ai_resolve.py`:
- `meta-website-verify` — which candidate domain is really the brand's (noon.com vs noon.ae)
- `meta-interstitial-click` — which element passes a geo/age/cookie gate
- `meta-extract-facebook` — pull FB link the regex missed (redirect wrappers, JSON-LD)
- `meta-page-disambiguate` — pick the real page among Ad Library look-alikes

**Key files**
- `meta_ad_scraper.py` — routes, Playwright scraper, `clean_brand()`, `run()`/`main()`
- `ai_resolve.py` — loads SKILL.md bodies, calls `claude -p`, parses JSON, `product_titles()`
- `websearch.py` — `find_websites()` (deterministic probe), `ai_find_website()` (AI web discovery)
- product context = `SELECT DISTINCT title FROM products WHERE brand_key = norm_key(brand) LIMIT 15`

## Test recipe

```bash
python3 meta_ad_scraper.py "vivonu"          # Route A
python3 meta_ad_scraper.py "gymshark"        # Route B
python3 meta_ad_scraper.py "NutroTonic LLC"  # suffix strip -> Route A
HEADFUL=1 python3 meta_ad_scraper.py sporter # watch geo-gate handling
# AI steps only truly run in an authenticated `claude` shell (else they abstain).
```

## Cases handled (keep in sync with data/resolutions.jsonl)

| brand | page_id | route | verdict | notes |
|---|---|---|---|---|
| vivonu | 205565203654666 | website | ✅ correct | "Vivonu nutrition", 0 active ads |
| gymshark | 129669023798560 | vanity | ✅ correct | 3,800 ads, parser validated |
| NutroTonic LLC | 106748161895549 | website | ✅ correct | fixed by stripping "LLC" |
| lunakai | 100000705465507 | vanity | ✅ Page (0 ads) | plugin-confirmed PAGE; earlier "profile" theory was WRONG (real Pages start 1000… too) |
| lilycare | 103140809302535 | vanity | ✅ Page (0 ads) | plugin-confirmed PAGE; resolved_no_ads |
| lilicare | None | — | ❌ open | brand spelled "lilicare" in DB; site/handle not found |
| sporter | None | — | ❌ open | country interstitial; needs AI click (Route A render) |
| sporter_creatine | None | — | ❌ open | same brand variant |

## Done

- [x] `scrape_ads()` + `EXTRACT_JS` — scrape all ads from a page id (validated)
- [x] Route A/B/C resolver cascade
- [x] 4 AI skills + `ai_resolve.py` plumbing (mock-validated; safe abstain on auth failure)
- [x] Product-niche context from DB threaded into AI steps
- [x] `clean_brand()` — strip legal suffixes (LLC/Inc/Ltd…) before searching
- [x] AI website discovery wired for when deterministic guessing finds nothing
- [x] `resolved_via` field in output; per-run JSONL log
- [x] **Page-vs-profile guard** — `looks_like_profile_id()` flags personal-profile
      ids (`1000…`/`61…` numeric, or `profile.php?id=`/`/people/` URLs). Sets
      `profile_suspect` + `status=resolved_profile_suspect`; warns that a "0 ads"
      from such an id is a likely false negative. (Conservative: lilycare `1031…`
      is borderline and deliberately NOT flagged, to avoid false alarms on pages.)
- [x] **`data/resolutions.jsonl`** auto-appended one line per run (ts, brand,
      page_id, resolved_via, status, profile_suspect, counts, url, error).
      Backfilled with the 8 historical cases. This is the queryable run index;
      `data/<brand>_meta_ads.json` holds the full per-case ads.
- [x] **Output status field** distinguishes outcomes: `resolved` /
      `resolved_no_ads` (genuine green-prospect signal) /
      `resolved_profile_suspect` / `unresolved` (resolution gap — NOT "0 ads").
- [x] **Flowchart + scenario-trace audit** (drew every micro-step, traced ~9 real
      brands through it) caught & fixed 3 logic bugs:
  - **`_dom` lstrip bug** — `lstrip("www.")` ate leading `w`/`.` chars
    (`weruva.com`→`eruva.com`, `watchshop.com`→`atchshop.com`), silently killing
    Route A for any `w`-leading brand domain. Now uses `removeprefix("www.")`.
  - **Unknown count treated as zero** — `(total or 0)==0` labeled an unparsed
    `_result_count`=None as `resolved_no_ads` (false green prospect, violates the
    "never treat unknown as zero" rule). Now `total==0` only is green; `None` →
    new `resolved_unknown_count`.
  - **Vanity route had zero verification** — it accepted ANY page id a *guessed*
    handle yielded. Now the page-plugin (`plugins/page.php`, which renders only
    real PAGES) is the provenance signal: a vanity hit confirmed by the plugin
    is accepted as route=`vanity`; an unconfirmed hit (raw/mbasic only, or an
    explicit profile URL) is held as a *weak fallback* and Route C runs first,
    used only if C fails (route=`vanity-unverified`).

- [x] **20-brand live test → 2 more bugs found & fixed** (`data/resolutions.jsonl`):
  - **Broken name-based verification** — my first vanity fix read the page NAME
    from the plugin HTML, but anonymous fetches return the generic title
    "Facebook", so `brand_name_matches` false-failed and demoted CORRECT pages
    (manscaped 280 ads, hexclad 1100 ads) to `vanity-unverified`+suspect. Dropped
    name-matching entirely in favour of the plugin-source provenance signal.
  - **Numeric profile heuristic was simply WRONG** — real Pages reached via the
    trusted website route (brooklinen `100064…`, native `100063…`) were flagged
    as profiles by the `1000…` prefix rule. Verified against Facebook: real Pages
    carry 1000…/100000… ids; the plugin endpoint even resolves lunakai/lilycare
    as Pages. Removed the prefix rule (`looks_like_profile_id` → deleted),
    replaced by `is_explicit_profile_url` (only profile.php / /people/ paths) +
    plugin provenance. lunakai/lilycare reclassified from "profile suspect" to
    legitimate Pages with 0 ads.
  - Result: 16/20 resolved; the 4 misses are domain-name coverage gaps (safe
    failures → status `unresolved`, never a false "0 ads"). Zero false profile
    flags after the fix.

## Open / next

- [ ] **lilicare**: investigate real site/handle (spelling vs lilycare).
- [!] **IMPORTANT — the 20-brand batch ran with AI OFF.** `claude -p` returns
      401 in the agent/subprocess shell, so EVERY AI step (website-verify,
      AI-discovery, interstitial-click, page-disambiguate) abstained. The 16/20
      that resolved did so on purely DETERMINISTIC paths; the 4 "unresolved"
      (caraway, olipop, Liquid Death, mudwtr) are AI-gated and should be re-run
      in the authenticated `claude` shell before treating them as failures.
- [x] **Diagnosed caraway** (= "Caraway Home" cookware, carawayhome.com):
      Route A impossible — its site has NO facebook link in the DOM at all;
      Route B impossible — real handle `carawayhome` ≠ variants of `caraway`;
      Route C is correct (typeahead top row `Caraway @carawayhome · Home goods
      shop` among 11 look-alikes: bands/realtor/politician/reseller) but needs
      meta-page-disambiguate, which was 401-abstaining. Expected to resolve in
      the authenticated shell via product-context (cookware) disambiguation.
- [x] **Route A AI-discovery escalation**: `resolve_via_website` now falls back
      to `discover_website_ai` when the deterministic domains all fail to yield a
      page id (not only when zero candidates were found). Helps the
      olipop/mudwtr class (non-bare-name domain that DOES carry an FB link).
- [ ] **Re-run the 4 AI-gated brands in the authenticated shell** to confirm
      caraway/olipop/Liquid Death/mudwtr resolve; then consider common-suffix
      domain guesses (drink-, get-, -home, -co) so Route A/B don't depend on AI.
- [ ] **sporter**: validate the geo-gate path (interstitial click) end-to-end in
      an authenticated shell.
- [ ] Optional: `--debug` flag printing the exact JSON payload sent to each skill.
- [ ] Wire the resolver into `pipeline.py` / `auto.py` so the background worker
      uses it (paid-API track was the earlier alternative; this is the scrape track).

## Google ad scraper (`google_ad_scraper.py`) — built this session

Companion to the Meta scraper, same contract (code drives Playwright; AI only
judges; abstain when unsure). Google is EASIER than Meta because the Ads
Transparency Center has **no login wall** and is keyed **directly on the
advertiser's web DOMAIN** — and we already solve brand→domain for Meta.

**Shared module `brand_web.py`** — pulled UA/http_get/clean_brand/_dom/
rank_websites/discover_website_ai out of meta_ad_scraper into one module so both
scrapers resolve brand→domain identically. `meta_ad_scraper.py` now imports them
(verified still works). New convenience `brand_domains(brand, products)` returns
best-first domains + note.

**Step 1 — resolve advertiser**
- ROUTE A (domain, no AI needed): `brand_domains` → load
  `adstransparency.google.com/?region=anywhere&domain=<d>`. The domain IS the
  advertiser key; provenance is unambiguous. A domain aggregates MULTIPLE
  advertiser accounts (correct per-brand signal). Scans candidate domains and
  PREFERS one that actually runs ads over a same-name zero.
- ROUTE B (name search + AI, fallback when no domain): type the brand in the TC
  search, collect advertiser suggestions as `/advertiser/AR…` links, and the new
  `google-advertiser-disambiguate` skill (via `ai_resolve.disambiguate_advertisers`)
  picks the brand's account; abstains when unsure.

**Step 2 — count ads.** KEY DISCOVERY (overturns the old `adscrape.py` "no
total-count label" assumption): the domain/advertiser view renders Google's OWN
total — "0 ads", "~37 ads", "~3k ads". We parse that label (`TOTAL_ADS_RE`) as
the authoritative headline count. Card/JSON-creative counts (lower bounds) are
only a fallback. A page that fails to load → no label, no cards → None
(UNKNOWN), never a guessed zero.

**False-green guard (Google-specific).** On Meta a wrong domain fails safely (no
FB link); on Google a wrong bare-name domain silently shows "0 ads" → a false
green. `websearch.find_websites` is only a TLD-variant guesser (caraway→
caraway.com/.us/.net), it does NOT find carawayhome.com / nativecos.com — those
need the AI discovery path (401 in the agent shell). So a zero is a confident
green ONLY when the domain was AI-verified/-discovered (`domain_verified`);
otherwise status is `resolved_no_ads_unverified` (NOT a clean green).

**Output**: `data/<brand>_google_ads.json` (full) + one line per run in
`data/google_resolutions.jsonl` (ts, brand, domain, advertiser_ids,
resolved_via, domain_verified, status, ads_count, url).

**20-brand live test (AI OFF — deterministic domain route only):** 20/20 reached
a domain + authoritative count, 0 "unknown".
- 13 `resolved` with real Google-labelled totals: gymshark ~30k, ruggable 5k,
  bombas 4k, ridge/brooklinen/drsquatch/hexclad(2k)/mudwtr(600)/magicspoon(97)/
  nutrotonic(37)/vivonu(17)/liquiddeath(4)/native.us(3).
- 7 zeros correctly held as `resolved_no_ads_unverified` (olipop.com verified-by-
  eye as a genuine "0 ads"; caraway.com/manscaped.us = wrong bare-name domain;
  weruva/lunakai/lilycare/trueclassic). None falsely sold as a green prospect.
- In the authenticated `claude` shell, AI domain-verify will confirm the genuine
  zeros (→ `resolved_no_ads`) and correct the bare-name misses (caraway→
  carawayhome.com, native→nativecos.com, manscaped→manscaped.com) to real counts.

| brand | domain | ads | status | note |
|---|---|---|---|---|
| ridge | ridge.com | ~3000 | resolved | label "~3k ads" |
| gymshark | gymshark.com | ~30000 | resolved | label "~30k ads" |
| olipop | olipop.com | 0 | resolved_no_ads_unverified | TC literally shows "0 ads / No ads found" — genuine 0 |
| caraway | caraway.com | 0 | resolved_no_ads_unverified | WRONG domain (real = carawayhome.com); AI fixes in auth shell |
| native | native.us | 3 | resolved | scan preferred ad-runner over native.com(0) |

### Google — open / next
- [ ] **Re-run in the authenticated `claude` shell** so AI domain-verify/discovery
      runs: confirm olipop/weruva genuine zeros (→ resolved_no_ads) and that
      caraway/manscaped/native resolve to their REAL domains with real counts.
- [ ] **`websearch.find_websites` is a TLD-guesser, not search** — it misses
      non-bare-name domains and even dropped manscaped.com (probe fragility).
      Consider a real search backend or common-suffix guesses (-home, -cos, -co).
- [ ] Optionally harvest per-ad creative details (currently `ads: []`; only the
      count is collected — sufficient for the <30 green-prospect signal).
- [ ] Wire google_ad_scraper into `pipeline.py`/`auto.py` alongside the Meta one.

## Decisions log

- Reuse local `claude -p` hook, no API key (matches websearch.py). AI abstains → fall through.
- Dropped open-ended agentic browser loop (token-heavy, non-deterministic). AI decides, code acts.
- Original brand name → DB product lookup; cleaned brand → web/handle/typeahead search.
- Accept a domain only if it actually yields a page id (deterministic safety net even when AI is down).
- Brand→domain logic lives once in `brand_web.py`; both scrapers import it so they can't drift.
- Google count = Google's OWN "N ads" label (authoritative), not card-counting; unparseable = None, never 0.
- Google "0 ads" is a confident green ONLY from an AI-verified domain; an unverified bare-name zero is
  `resolved_no_ads_unverified` (a wrong domain shows a real-but-irrelevant 0 = false green). Mirrors Meta's
  "a wrong answer is worse than none".
