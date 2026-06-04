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
