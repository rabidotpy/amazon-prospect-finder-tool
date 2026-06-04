# Audit Plan — Amazon Prospect Tool (6 angles, evidence-driven)

This plan was seeded by **running the system** against controlled scenarios and **metering real
calls**, then turning the observed patterns into a repeatable, strict audit. Each angle states:
the **objective**, the **scenarios/variations** to exercise, the **metrics + pass/fail
thresholds**, the **method/tools**, and the **seed evidence already observed** (so the next pass
starts from facts, not a blank page).

> Empirical harness already built: `/tmp/aptool_audit/scenarios.py` (isolated temp-DB scenario
> matrix) and `/tmp/aptool_audit/meter_ai.py` (metered `claude -p --output-format json`). Reuse and
> extend these.

---

## Angle 1 — Calculation correctness

**Objective:** every number that drives a lead decision (brand/seller revenue & sales, counts,
green threshold, freshness math) is provably correct, with explicit NULL/dedupe/rounding behavior.

**Scenarios & variations to run** (extend `scenarios.py`):
- Parent-revenue dedupe matrix: (a) one parent across N child ASINs; (b) **two distinct parents,
  equal revenue**; (c) two parents, different revenue; (d) same parent across two sellers; (e) same
  parent across two brand spellings. Assert brand/seller totals against a hand-computed truth table.
- Seller `brand_count`/`brands`: brand names containing `,`, `;`, unicode, very long, duplicate
  brand under one seller, 1000 brands under one seller (GROUP_CONCAT length cap).
- `min_listing_age`: all-null ages, age=0, age=6, age=6.5 → does `prospect_signal=new` fire correctly?
- Number parsing: `"$1,234.56"`, `"1,234"`, `""`, `"N/A"`, `"-"`, negative, scientific notation, in
  **both** CSV and XLSX. Assert stored dtype is numeric.
- Green threshold boundaries: ads at `MAX-1`, `MAX`, `MAX+1`, NULL, 0.
- Freshness math: `_stale` at exactly 30 days, DST boundary, future timestamp, malformed timestamp.

**Metrics / thresholds:** 0 discrepancies vs the truth table. Any silent collapse or string-typed
numeric = fail.

**Seed evidence (already observed):**
- ✅ **Equal-revenue parents collapse** → two real $1000 parents report brand revenue **$1000** (S3).
  Root cause: `db.aggregate_sellerbrand` groups by the **value** because the Helium10 export has **no
  Parent ASIN** column (verified). True magnitude unknown without parent IDs.
- ✅ **Comma in brand mangles seller rollup**: `brand_count=3` for **1** brand; `brands=" LLC, …"`
  (S2). `GROUP_CONCAT(DISTINCT brand)` + `.split(",")`.
- ✅ **`price` not coerced** (`FLOATCOLS` excludes it) — `"$1,234.56"` stored as TEXT (S9). Latent:
  current export prices are all float, but a `$`/comma export breaks price filter/sort.
- ✅ Child-ASIN repeat dedupe is correct (S4); blank-ASIN rows skipped (S5).

---

## Angle 2 — Accuracy (are the outputs *true*?)

**Objective:** the website, ad-count, and green outputs reflect reality; "unknown is never zero"
holds; AI abstains instead of guessing.

**Scenarios & variations:**
- Build a **labeled brand set** (~40): known-real-DTC, known-parked (`brandname.com` for sale),
  marketplace-only (sells only on Amazon/iHerb), ambiguous name (shares name with unrelated co.),
  regional (`brand.com` vs `brandusa.com`), and no-site. Run website resolution; score
  precision/recall vs labels. Target: **precision ≥ 0.95** (a wrong site is worse than none),
  recall measured and reported.
- Parked detector unit set: 30 known parked landers (GoDaddy `/lander`, Afternic, Sedo, Dan, expired)
  + 30 real stores. Measure FP/FN. Target FP on real stores **= 0**.
- "Unknown is never zero": force scraper failure/timeouts; assert `meta_ads_count`/`google_ads_count`
  persist as **NULL**, never 0, and `is_green` does **not** flip on unknowns.
- Brand↔seller↔product mapping: spot-check 20 brands' `brand_key` normalization (case, punctuation,
  unicode, "LLC"/"Inc" suffixes) for false merges/splits.
- AI determinism: run the same brand 3× — is the chosen domain stable? Flag nondeterminism.

**Metrics / thresholds:** website precision ≥ 0.95; parked-detector FP = 0; 0 cases of NULL→0; 0
false brand merges in the sample.

**Seed evidence:**
- ✅ Website research returns correct non-guessable domains (NOW Foods→`nowfoods.com`,
  Ascent→`ascentprotein.com`, Animal→`animalpak.com`) and **abstains** when unsure (VEV→UNKNOWN,
  Nutrition On The Go→none). Olipop→`drinkolipop.com`, ZzzQuil→`zzzquil.com`.
- ✅ **False-positive green confirmed**: no website + **NULL** ad counts → `is_green=1` (S7). Accuracy
  hole the moment any scrape is incomplete.
- ⚠️ **Open question:** metered research showed `server_tool_use.web_search=0` on all calls. Multi-turn
  growth (up to 8 turns / 138k cache) suggests the CLI's **client-side** WebSearch is used (not
  counted server-side) — but this must be **confirmed**: is research truly browsing/verifying, or
  recalling from Opus memory? Audit by diffing answers with/without `--allowedTools`.

---

## Angle 3 — AI token consumption / cost

**Objective:** quantify $ per run and per call, find waste, and define a cost model with a budget.

**Scenarios & variations:**
- Meter (reuse `meter_ai.py`) across brand types (famous, obscure, ambiguous, no-site) × models
  (`--model` opus vs sonnet vs haiku) × with/without web tools × with/without the deterministic
  pre-gate. Record `total_cost_usd`, tokens, cache split, turns, latency, correctness for each cell.
- Measure the **fixed CLI overhead**: the cache/scaffolding tokens `claude -p` loads per call (CLAUDE.md,
  tool defs) and whether a leaner prompt / `--model` reduces it. (We stay on the **claude-CLI
  subscription, not an API key** — so this overhead is a **quota/latency** tax, not a cash one.)
- Full-run projection at concurrency 2/4/8 incl. re-queue/re-scan multipliers; model a monthly cost
  under realistic refresh cadence.
- Narrow-skill cost × 5 agents × scraper invocation frequency.

**Metrics / thresholds:** define a target **≤ $X per full run** and **≤ $Y/day** budget; require a
hard ceiling in code. Flag any path with no cap.

**Seed evidence (metered, real $):**
- 🔴 **Research = $0.256/brand avg → ≈ $324 per 1,267-brand run**, on **Opus 4.8** (CLI default).
  Range $0.16–0.35, 19–55s, 3–8 turns.
- 🔴 **Narrow skill = $0.042/call** on Opus (inlines full SKILL.md each call).
- 🔴 **Per-call overhead is dominated by Claude Code scaffolding**: 16k–138k **cache** tokens are the
  CLI's own system context, not our ~500-token prompt.
- **Levers to audit/quantify:** (1) **model tiering** — Haiku ~15× cheaper than Opus for narrow
  picks; Sonnet for research — via `claude -p --model`, staying on the **CLI subscription, not an API
  key**; (2) **trim the per-call scaffolding** (leaner prompt) to reduce the quota/latency tax; (3)
  **gate research behind the cheap `find_websites` probe** (skip
  AI when a real, non-parked `brandname.com` already verifies); (4) **don't re-run** confirmed
  (`confidence='ai'`) brands; (5) prioritize spend by revenue (only research brands above a revenue
  floor).

---

## Angle 4 — Performance

**Objective:** identify current and scale-risk bottlenecks; set latency budgets.

**Scenarios & variations:**
- Synthetic scale: 10k / 50k / 200k products. Time full-table pandas loads, `run_search`,
  `run_advanced`, `rebuild_sellers`, `stale_brand_keys`, green recompute. Plot growth.
- Streamlit interaction cost: rerun count per keystroke; uncached vs `ttl` cached main-page loads.
- Worker throughput: brands/hour at concurrency 2 vs 4; where is wall-clock spent (AI vs Playwright
  vs DB); effect of a single hung child on the in-process pool.
- DB: `EXPLAIN QUERY PLAN` for every hot query; index coverage; WAL contention under 2 writers + N
  dashboard readers.

**Metrics / thresholds:** dashboard interaction < 200ms at 10k rows; no query doing an unindexed scan
on the hot path at scale; worker throughput documented; a single stuck brand must not stall others.

**Seed evidence:**
- ✅ Current scale is tiny and fast: loads 29/26/4ms; search over 2k rows ~50ms. **Not a bottleneck
  today.**
- ⚠️ Full-table `SELECT *` → pandas on **every** rerun (main page loaders have **no TTL**, unlike the
  green page) — O(n) per interaction; ~10× data = ~300ms × every click.
- ⚠️ `stale_brand_keys` does `SCAN brands` + `TEMP B-TREE` (no index on `parent_level_revenue`);
  `rebuild_sellers` `TEMP B-TREE FOR GROUP BY`. Fine now, indexable later.
- 🔴 **Real worker cost is AI/Playwright latency**, not SQL: research 19–55s/brand dominates; at
  concurrency 2 a full pass is hours. This is the true performance lever (see Angle 3 gating).

---

## Angle 5 — Business solution fit

**Objective:** judge whether the output actually helps book outreach, not just whether code runs.

**Scenarios & variations:**
- Take the top-50 green prospects and ask, per row: is this a *real* opportunity (proven Amazon
  revenue + genuinely no/low DTC + reachable)? Score actionability. What % are noise (tiny revenue,
  $0 rows, non-brand "watch" rows, foreign micro-sellers)?
- Walk a rep's real workflow: from a green row, how many clicks/steps to a contact and a pitch?
  What's missing (email/owner discovery, LinkedIn, "why now" signal, estimated DTC upside)?
- Definition stress test: does "green = no website AND <30 ads" match the ICP? Should it weight
  revenue, review velocity, listing age, category? Are `$0`-revenue "watch" rows polluting the list?
- Dedupe across repeated Helium10 imports over weeks (brand renames, new ASINs): does the list stay
  trustworthy? (ties to Angle 1 pruning.)

**Metrics / thresholds:** % of top-50 that a rep would actually contact (target high); steps-to-
contact; coverage of revenue concentration (are we surfacing the few high-value leads, not 1,130
marginal ones).

**Seed evidence:**
- ⚠️ Green list is **1,130** brands after the recent clear — many `$0` revenue / `watch` rows. Volume
  over signal; needs a revenue/quality floor and ranking, not just a boolean.
- ⚠️ No contact/owner discovery, no CRM handoff wired (`crm_export.py` appears dead), no "why this
  lead" rationale — the output is a list, not a worklist.
- 🔴 Spending **$324 of AI** to website-qualify 1,267 brands when only the high-revenue tail is worth
  pitching is a **business-cost misalignment** (ties Angle 3 ↔ 5): gate spend by revenue.

---

## Angle 6 — Best practices / production-readiness

**Objective:** would this survive unattended for a month and a new teammate taking it over.

**Scenarios & variations / checklist:**
- **Tests:** there are **zero** automated tests. Stand up a pytest suite from the scenario harness
  (calculation truth tables, parked detector, green NULL-safety, ingest dedupe). Target: the P0/P1
  calculation+accuracy bugs each get a regression test.
- **Security:** `research_website` runs `--permission-mode bypassPermissions` with WebFetch of
  untrusted pages while user MCP servers (Airtable/ClickUp/LinkedIn/Meta-Ads/Chrome) are co-resident
  and reachable; prompt-injection surface. Audit: explicit allowlist, `--strict-mcp-config`, drop
  bypass, domain post-verification, "content is data" guard. Also: live `FB_ADLIB_TOKEN` on disk but
  unused; `todo-done.md` tracked in the public repo.
- **Reliability:** no per-brand timeout (one hung child freezes the in-process worker); no retry/
  backoff on transient AI 429 or scraper errors → hot-loop; SIGTERM doesn't interrupt the pool.
- **Cost governance:** no budget ceiling, no per-run cap, no metering/telemetry of spend.
- **Config & reproducibility:** hardcoded paths, default-Opus, no model/budget config; `requirements`
  pinning; dead code (`resolver/`, `extension/`, `adscrape.py`, `crm_export.py`).
- **Observability:** structured logs, per-brand outcome trail, a spend dashboard.

**Metrics / thresholds:** a green CI test run; no `bypassPermissions` in the unattended path; a hard
cost ceiling enforced in code; named owner for each dead module (wire or delete).

**Seed evidence:** 0 tests; `bypassPermissions` in worker; no cost cap; dead modules present;
`FB_ADLIB_TOKEN` unused; repo-hygiene items confirmed.

---

## How to execute this audit (next pass)

1. **Extend the two harnesses** into a `tests/` pytest suite (calculation + accuracy + parked).
2. **Run the metering matrix** (Angle 3) to pick the model/transport (CLI vs API) and the gating
   policy — this single decision moves cost from ~$324 to an estimated $5–25/run.
3. **Build the labeled brand set** (Angle 2) and score website precision/recall before/after any
   change.
4. **Synthetic-scale** the DB (Angle 4) to set latency budgets.
5. **Score top-50 actionability** (Angle 5) with a rep's lens.
6. Roll findings into a single ranked remediation plan (impact × effort), superseding `FIXPLAN.md`.

**Cross-angle tension to resolve up front:** accuracy (open-ended AI research) ⟂ token cost ⟂ worker
performance. The business angle breaks the tie — **spend AI only on revenue-qualified brands**,
tier the model, and gate behind the deterministic probe.
