# Website-Resolution Accuracy Report — 100-Brand Live Sample

**Method.** Drew a random 100-brand sample (seed=42) from the live `brands` table (1,267 brands),
ran the **real** website researcher (`ai_resolve.research_website`, the production path) on each with
`--output-format json` to capture cost, then **independently verified every output**: live-fetched
the returned domain, ran the parked/redirect detector, and checked the brand token against the page
title. Questionable verdicts were re-fetched by hand with a browser user-agent and judged manually.
Raw data: `/tmp/aptool_audit/sample100.jsonl` (one JSON line per brand).

---

## Headline numbers

| Verdict (automated) | n | Meaning |
|---|---|---|
| **OK** | 58 | live site, brand name matches |
| **ABSTAINED** | 33 | AI returned no DTC site |
| **DEAD_OR_BLOCKED** | 5 | returned domain didn't fetch for my verifier |
| **NAME_MISMATCH** | 4 | live site, but name doesn't match the brand |

**Cost:** $22.51 for 100 → **≈ $285 for a full 1,267-brand run**. Avg **$0.225/brand**, **27.2s** each,
all on **Opus 4.8**. `server_tool_use.web_search = 0` on **all 100** (research relies on client-side
tools / parametric memory, not server-side web search).

> **Billing note (we run on the claude-CLI subscription, not an API key).** These $ are the
> **API-equivalent** cost the CLI reports; on the flat-fee subscription they are **not cash per run**.
> The binding constraints are therefore **rate-limit / quota consumption and latency**, not dollars.
> The optimization levers below still apply — they cut quota burn and wall-clock — but read "$285" as
> "the quota/latency weight of running Opus on everything," not a bill.

### Adjusted accuracy (after manual verification)
The automated verdicts undercount true accuracy because my verifier gets **bot-blocked** on real
sites:
- `DEAD_OR_BLOCKED`: `nicorette.com` (SSL), `legionathletics.com` (403) are **real, correct sites** —
  AI right, verifier blocked. `yolobalance.store` (402) and `holytime.net` (502) are real-but-down/
  flaky. So ~3–4 of these 5 are **AI-correct**.
- `NAME_MISMATCH`: all **4 are genuine AI errors** (see below).

**Reclassified:** ~**61–62 correct finds**, **~4 wrong**, **33 abstentions** (mostly correct).
→ **Precision on confident answers ≈ 62 / 66 ≈ 94%** (just under the 95% bar), **wrong-site rate ≈ 4%**.

---

## The 4 wrong answers (manually confirmed — the real defect)

| Brand | AI returned | Reality | Failure mode |
|---|---|---|---|
| Weslaplus | `olavitaskin.com` | "Olavita Skincare" — a **different brand** | category-match, wrong brand |
| FKMEE | `walkerskinprotect.com` | "Walker Skin Protect" — **different brand** | category-match, wrong brand |
| Golden Throat | `solsticemed.com` | a TCM **reseller** that carries the product | reseller ≠ brand's own store |
| Health Solution Prime | `vitaminprime.com` | "Vitamin Prime" — **different brand** (probable) | name-overlap, wrong brand |

**Pattern:** errors are confident, plausible, and all on **obscure / low-revenue, Amazon-only**
brands. When no real DTC site exists, the researcher sometimes latches onto a **same-category** site
(skincare→skincare, supplement→supplement) or a **reseller**, instead of abstaining. These are the
dangerous errors — a wrong site looks authoritative in the dashboard.

---

## What's working well

- **High-revenue brands resolve correctly.** Of brands >$50k revenue: **9/9 found a real site**
  (the 2 "DEAD" were real sites blocking my probe). The valuable leads are accurate; errors are on
  the long tail of noise. (OK-brand median revenue $1,354 vs abstained $343.)
- **Non-guessable domains found:** RAW→`getrawnutrition.com`, Momentous→`livemomentous.com`,
  Animal→`animalpak.com`, Ascent→`ascentprotein.com`, BIRDMAN→`birdman.com`, Himalaya→
  `himalayausa.com`, NOW Foods→`nowfoods.com` — none are `brandname.com`; the old deterministic
  guesser would have missed every one.
- **Abstention is mostly correct and on-mission.** The 33 abstentions are dominated by random-letter
  Amazon-only brands (WSJAWH, LTQXGT, SBHEANGBA, Dvloge…) with no DTC site. For a *green-prospect*
  tool, "no DTC site" **is the target signal** — these abstentions are a feature, not a failure.

## What's wrong / risky

1. **~4% confident wrong-site rate** on obscure brands (table above). Precision ~94%, below the 95%
   target. Fix: tighten the prompt to **reject same-category-but-different-brand and resellers**, and
   add a post-check (returned domain's site must contain the brand token, not just the category).
2. **Nondeterminism.** Same brand → different answers across runs: **Nutrition On The Go** abstained
   earlier but returned `nutritionotg.com` (OK) here; **VEV** abstained in metering but returned
   `vevnutra.com` here. The determine-once rule then **locks in whichever answer happened first** —
   so a brand's stored site is partly luck-of-the-run. Fix: lower temperature / add a confirmation
   pass for borderline cases before locking.
3. **Some over-abstention misses.** `thevofel.com` is a real VOFEL store, but VOFEL abstained.
   A few real sites are dropped as "Amazon-affiliated only."
4. **`web_search=0` on all 100.** The "researcher" isn't doing server-side web search; it leans on
   Opus's memory. This explains both the famous-brand accuracy **and** the obscure-brand guessing
   (no memory → confabulate a plausible site). If we want true verification, server-side web_search
   must actually fire.
5. **Cost: ~$285/run, all Opus, even to say "no site".** Every one of the 33 abstentions cost ~$0.22.

---

## Recommendations (priority order)

1. **Gate spend by revenue + cheap probe.** Only invoke the AI researcher for brands above a revenue
   floor or where the deterministic probe is empty/ambiguous. The high-value leads (which resolve
   accurately) are a small fraction; the long tail (where errors and abstentions concentrate) isn't
   worth $0.22 each. Biggest single win on cost **and** error exposure.
2. **Tier the model — staying on the claude-CLI subscription (no API key).** Pass `--model` to
   `claude -p`: Haiku for the narrow picks, Sonnet for research, instead of the default Opus. This
   cuts quota burn and latency sharply (Haiku ≈ 15× lighter than Opus) without leaving the
   subscription. Combined with #1, the effective quota/latency weight drops to a small fraction.
3. **Harden the prompt + post-verify the domain** to kill the same-category/reseller wrong-site
   errors (must match brand token, not just category; explicitly reject resellers/marketplaces).
4. **Reduce nondeterminism** before determine-once locking (low temperature; a second confirmation
   call for borderline/obscure brands, or require the site to actually be fetched & brand-matched).
5. **Make web_search actually fire** (or accept it's memory-based and label confidence accordingly).
6. **Improve the verifier** (browser UA, follow blocks) so QA doesn't false-flag real sites like
   Nicorette/Legion.

**Bottom line:** for the leads that matter (real revenue), the system resolves websites accurately
and finds non-obvious domains the old method never could. The weaknesses are concentrated on the
low-value long tail — confident wrong sites (~4%), run-to-run nondeterminism, and paying Opus rates
(~$285/run) to qualify brands most of which aren't worth pitching. Fixing those is mostly about
**spending AI only where it pays off** and **tightening the brand-vs-category guard.**

---

# Sample #2 — Stored-data verification (fresh random 100, manual)

**Method.** New random sample (seed=7) from the **474 brands with a committed website**
(`confidence='ai'`). Each stored site was **live-fetched with a real browser UA** (so bot-blocked
real sites aren't false-flagged like Sample #1), parked-checked and brand-matched, then a **DB-wide
census** counted each issue class exactly. Raw: `/tmp/aptool_audit/verify100.jsonl`.

**Sample verdicts (n=100 stored sites):** OK **73**, LIVE_BLOCKED **26** (all real — Jack Black→
jackblack.com, Solaray→solaray.com, Peter Thomas Roth, Animal→animalpak.com…), PARKED **1** (false
flag, Issue D), WRONG_BRAND **0**. → **Stored website data is ~99% real.** The serious problems are
found by the census:

## Issue A — [CRITICAL] Green list is 97% unverified (NULL ad counts scored as 0)
- **Wrong:** `is_green=1` asserted for brands whose Meta/Google ad counts are **unknown**, not zero —
  the core deliverable is mostly unproven.
- **Numbers:** **781** green; only **22** have **both** counts known; **759 (97%)** green on ≥1
  unknown count; **583** never Meta-scanned.
- **Why:** `green.is_green` uses `(meta_count or 0) >= MAX` → Python maps `None`→0 ("not scraped" ==
  "zero"). Plus coverage: even where Meta *was* scanned (683), the scraper returns unknown ~**41%** of
  the time (only 402/683 produced a number).
- **Affected records:** 759 rows `is_green=1 AND (meta_ads_count IS NULL OR google_ads_count IS NULL)`;
  583 of them never Meta-scanned.
- **Fix:** `is_green` must require **known** counts — when `has_website` is false but either count is
  `None`, return a distinct **NULL "candidate (ads unverified)"** state, never 1; gate the Green
  page's headline count on the existing **Verified** column.
- **Prevent:** unit test that `is_green` never returns 1 on NULL counts; a "Candidate vs Verified"
  badge so the two can't be confused.

## Issue B — [HIGH] Confident wrong-brand websites persisted (same-category trap)
- **Wrong:** obscure Amazon-only brands with no DTC site get a **different brand's** same-category
  site, and `determine-once` **locks it** (`confidence='ai'`).
- **Numbers:** token-mismatch census **15/476 (3%)**, but most are **legit** (abbreviations
  `MHP→mhpstrong.com`, `Premier Research Labs→prlabs.com`, `5%→5percentnutrition.com`; subdomains
  `SOHM→store.sohm.com`; parent makers `Preggie Pop Drops→threelollies.com`). **Genuinely wrong ~3–5:**
  `FKMEE→walkerskinprotect.com` (verified "Walker Skin Protect"), `BIZYAC→freshliving.store`,
  `Tbexem→daislashes.com`. True stored wrong-brand rate ≈ **0.6–1%** (≤4% upper bound).
- **Why:** prompt allows category-match without a hard brand-token match; `web_search=0` → model
  confabulates from memory for unknown brands; no post-verification before persisting.
- **Affected records:** the ~3–5 named; ≤~19 across 476.
- **Fix:** **post-verify** in `resolve_web` — returned domain/title must contain the brand token
  (allow abbreviation/subdomain/`&`/digit normalization); else abstain. Prompt rule: reject
  same-category-different-brand and resellers; UNKNOWN if no own DTC site.
- **Prevent:** brand-token gate on every write; regression fixtures BIZYAC/FKMEE/Tbexem.

## Issue C — [HIGH] Nondeterminism + lock-in
- **Wrong:** same brand → different answers; `determine-once` stores whichever **first confident** run
  produced. `Weslaplus`→`olavitaskin.com` (Sample #1) vs **abstain** now; `VEV`, `Nutrition On The Go`
  flipped too. A wrong first run is locked (FKMEE).
- **Affected:** any brand whose first confident run was wrong (Issue-B set is the realized subset).
- **Fix:** lower temperature; for borderline/obscure brands require a **confirmation run** to agree
  before locking, else leave pending.
- **Prevent:** never `ai`-lock a result that failed the brand-token gate or wasn't reproduced.

## Issue D — [MEDIUM] Parked-detector false positive on a real store
- **Wrong:** `Natures Craft→shopnaturescraft.com` (real Shopify store) flagged **PARKED**.
- **Why:** over-broad `("buy now"|"make an offer")+"for sale"` combo and `"premium"` markers fire on
  real e-commerce. In the live path this can **reject a correct AI domain** as parked → real site lost.
- **Affected:** ~1/100 sample; any store using those words.
- **Fix:** make **redirect-to-parking-path** (`/lander`…) the primary signal; drop the generic combo
  and bare `"premium"`; keep registrar signatures + explicit "this domain is for sale".
- **Prevent:** parked test set (30 real stores incl. supplement Shopify) with **FP=0** gate.

## Issue E — [MEDIUM] Calculation (re-measured)
- **192** `(seller,brand)` groups have >1 ASIN at identical `parent_level_revenue` (some legit child
  repeats, some true distinct-parent collapse — unprovable without Parent ASIN, which the export
  lacks). Comma-in-brand sellers **1**; `price` TEXT **0** — both latent; still need the
  parser/`GROUP_CONCAT`/`parent_asin` fixes to stay safe on future imports.

## Issue F — [MEDIUM] Coverage presented as completeness
- **625** brands website-pending; **583** green brands never Meta-scanned; **121** products with blank
  `seller_key` (in brand revenue but no seller row). **Fix:** show per-signal coverage on the
  dashboard; exclude ads-unverified brands from the headline green count (ties to Issue A).

### Sample #2 bottom line
Committed **website data is ~99% accurate** (~1% wrong-brand) — better than Sample #1's fresh-run 4%
because nondeterminism turns some wrong runs into abstains before locking. The **dominant
data-accuracy problem is the green flag: 759/781 (97%) green brands are qualified on unknown ad
counts** (`is_green` treats NULL as 0, and ad-scanning lags website research). That logic+coverage
gap — not website resolution — is what makes the core deliverable untrustworthy today.
