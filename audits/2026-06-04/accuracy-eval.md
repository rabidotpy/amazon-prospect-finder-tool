# Accuracy Evaluation — Website + Ad-Account (frozen 100-brand sets)

Empirical, manually-verifiable accuracy audit of the two most important signals, run as an
implement → test → verify loop against frozen 100-brand samples.

## What is independently verifiable (and what isn't)

| Signal | Verifiable? | Method |
|---|---|---|
| **Website** | ✅ Yes | live-fetch the stored domain (browser UA, follow redirect) + `brand_domain_match` + `is_parked` |
| **Google ad-account** (right advertiser) | ✅ Yes | the `?domain=` in the transparency URL must pass `brand_domain_match` |
| **Meta ad-account** (right FB page) | ⚠️ Partial | fetch `facebook.com/<page_id>` → public `og:title`; compare to brand (token match unreliable for same-word different-company, see Legion) |
| **Exact ad COUNTS** | ❌ No (here) | Google Transparency is JS-only (WebFetch sees no data); Meta Ad Library returns 403; FB Graph token expired; Graph `ads_archive` covers only political ads, not commercial. Counts depend on the Playwright scrapers + paid APIs. |

**Conclusion:** we can rigorously verify the **right account/website was attached** (the #1 way a count
goes wrong); the **count magnitude** is not machine-verifiable in this environment.

## Results

### Website accuracy
- **Before fix:** 96 OK_LIVE, 2 PARKED, 2 blocked-real → **98%** precision.
- The 2 parked (`covergirl.co`, `aceyo.com`) came from the `google_domain` **reconcile**, which skipped
  the parked check. Re-checking all 103 google-domain sites found **22 parked squatters** wrongly
  adopted as websites (`nestle.shop`, `generic.co`, `dxwxh.com`, `covergirl.co`, …) — all reset.
- **After fix** (reconcile now runs `is_parked`): 93 OK_LIVE + 7 blocked-real, **0 wrong-brand, 0
  parked → 100% precision** on a fresh 100. (Blocked = real sites that 403/SSL-block the probe, e.g.
  Jack Black, Solaray — confirmed real.)

### Google ad-account accuracy
- **100/100** resolved domains pass `brand_domain_match`. The transparency `?domain=` is consistently
  the brand's own domain.

### Meta ad-account accuracy
- **66 OK / 8 mismatch / 1 unfetchable** of 75 with a `fb_page_id` (~88%). Wrong pages seen:
  `Type Zero → AdornThemes` (a Shopify theme), `Doré → Hover`, `LEGION → Legion Technologies`
  (workforce software, not the supplement brand). **Most wrong pages have `meta_count = 0`**, so they
  don't corrupt a count — but they're the wrong account.
- Some "mismatches" are **eval artifacts** (e.g. `INNO INNOAESTHETICS → Innoaesthetics | Sant Just…`
  is actually correct; my crude name-vs-domain check mis-flagged it) and **native-script** pages
  (Korean/Chinese) that can't be string-matched.
- **String matching can't fix Meta**: `LEGION` (supplements) vs `Legion Technologies` both contain
  "legion" — only the **product context** distinguishes them, which is exactly what the
  `meta-page-disambiguate` AI skill is for. An automatic string gate would both miss Legion and
  false-reject native-script brands, so it is **not** added. Improving Meta-account accuracy means
  tuning that skill / preferring the website's own FB link over AI disambiguation — a separate,
  careful task, not a string hack.

## Fixes shipped this loop
- `reconcile_website()` now `is_parked`-checks the Google advertiser domain before adopting it as the
  website (regression test added). Re-backfill cleaned 22 parked adoptions.
- Net: website precision 98% → **100%**; the green list shed brands that only ever had a parked
  "website".

## Methodology / harness (reusable)
- `/tmp/aptool_audit/eval100*.json` — frozen samples; `eval_acc*.py` — website + Google verifier.
- Re-run after any website/ad change; target **website precision ≥ 98%**, **Google account = 100%**,
  and track Meta-account mismatch (flag any nonzero-count wrong page for manual review).
