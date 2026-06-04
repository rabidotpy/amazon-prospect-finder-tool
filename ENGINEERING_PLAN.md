# Engineering Plan — Amazon Prospect Finder

The staff-engineer view: **where we are, the target architecture, the order we build in and *why*, the
decisions that shape it, and how we know each phase is done.** Tactics live in
`audits/2026-06-04/fixation-plan.md`; this document is the altitude above it.

> **North star (from the README):** a *trustworthy, ranked, actionable, affordable* worklist of
> Amazon brands worth pitching. The green flag is the signal; **booked outreach is the goal.**

---

## 1. Current reality (honest assessment)

A research spike that became production by accident. The **pipeline works** — ingest → enrich
(AI website + ad scrape) → green flag → dashboard — and the hard, novel parts (AI website research,
parked-domain defeat, isolated concurrency) are solid. But it is **prototype-grade as a product**:

- **The deliverable isn't trustworthy.** 97% of greens are flagged on *unknown* ad counts (NULL→0).
- **No qualification model** — "green" is an ad-hoc boolean that conflates *unknown* with *zero*.
- **Cost/value is misaligned** — we spend equally on the $300k brand and the $300 one.
- **It's a list, not a worklist** — no ranking, no contact path, no "why this lead."
- **No safety net** — zero tests, ungoverned spend, `bypassPermissions`, no observability.

The audits already proved all of this empirically. The job now is to turn a working pipeline into a
**product a rep trusts and closes from**, without losing the parts that work.

---

## 2. Target architecture

Make the implicit pipeline explicit, with **clean stage boundaries** and an **honest state model**
(the root cause of the green bug is that one stage's "unknown" was silently read as another's "zero").

```
  Helium10 export
        │  INGEST          de-dupe on ASIN, coerce types, parent-aware revenue
        ▼
  products (facts)
        │  NORMALIZE       brand/seller rollup, pruning, revenue aggregation
        ▼
  brands / sellers (entities)
        │  ENRICH          orchestrator: revenue-prioritized, budget-aware, idempotent
        │   ├─ website     deterministic context → AI decision → post-verify → {site | none | unknown}
        │   └─ ads         meta/google scrape → {count | unknown}      ← never NULL-as-0
        ▼
  enriched brands (+ explicit per-signal state: verified | unknown | pending)
        │  QUALIFY + SCORE  green = (no site) ∧ (ads KNOWN ∧ low); rank by revenue × fit
        ▼
  worklist (read-model)
        │  DELIVER         dashboard · export · CRM/contact handoff
        ▼
  the rep
```

**Principles that fall out of this:**

1. **Explicit state, never coercion.** Every signal is `verified | unknown | pending` — qualification
   may only act on `verified`. This single rule kills the green bug class permanently.
2. **Deterministic gathers context; AI decides; code verifies.** Already the website pattern — make it
   the law everywhere AI is used (post-verify the AI's output against a deterministic check).
3. **Enrichment is a budget-aware orchestrator, not a loop.** It prioritizes by revenue, caps spend,
   backs off on failure, is idempotent and resumable. Cost/perf/accuracy tensions all resolve here.
4. **Presentation reads a clean read-model.** The dashboard renders a `worklist`, never re-derives
   business logic. (Today the green rule lives partly in the page.)
5. **AI is a bounded service on the subscription** — tiered (`--model`), tool-allowlisted, MCP-isolated,
   output-verified, metered. Never an API key; never `bypassPermissions`.

We are **not** rewriting — these are seams to refactor *toward*, one vertical slice at a time. SQLite +
Streamlit + the launchd worker stay; they're right for this scale.

---

## 3. Roadmap — phased by the end goal, with exit criteria

Each phase is a shippable increment with a **measurable done**. Order is deliberate: *make it true
before fast, fast before pretty, pretty before bulletproof.*

### Phase 0 — Stabilize & instrument *(so we can move safely)*
Stop the worker wedging and gain eyes before changing behavior.
- Per-brand timeout + clean SIGTERM (P0-1, P2-10); failure backoff (P1-1); robust JSON parse (P0-3).
- Scaffold `tests/` + CI; spend/coverage logging (G5 skeleton, G6).
- **Exit:** a stuck brand never freezes the worker; `pytest` runs green in CI; every run logs
  tokens + coverage.

### Phase 1 — Make the green list TRUE *(the core deliverable)*
The product is worthless if a green row lies. This is the priority.
- **Qualification state model**: `is_green` requires *known* ad counts; `unknown ≠ 0` (S2-1/P1-4).
- **Verify ad-count accuracy** — the one signal never audited; sample vs Meta/Google ground truth.
- **Website integrity**: brand-token post-verify before persist (S2-2); parked FP=0 (S2-3).
- **Exit:** a "Verified Green" row provably means *no DTC site + known-low ads*; ad-count accuracy
  measured ≥ target; wrong-brand rate < 1% with a regression test guarding each.

### Phase 2 — Make it AFFORDABLE *(quota, not $ — we're on the subscription)*
- Gate AI behind the cheap probe + a **revenue floor** (P1-6, G2); model tiering Haiku/Sonnet (G1).
- Budget ceiling + backoff in the orchestrator.
- **Exit:** a full refresh costs a small fraction of today's quota/latency; no brand below the floor
  consumes an AI call; spend is bounded and logged.

### Phase 3 — Make it ACTIONABLE & PRIORITIZED *(a worklist, not a list)*
- Rank green by revenue × fit; drop `$0`/`watch` noise from the headline (G2).
- "Why this lead" rationale; exportable worklist; a contact/owner-discovery path (G3).
- **Exit:** top-50 worklist is what a rep would actually call, in priority order, with a contact path —
  scored against the Angle-5 actionability rubric.

### Phase 4 — Correct the numbers *(quiet accuracy debts)*
- Parent-aware revenue (`parent_asin`, P1-2); comma-safe seller rollup (P1-3); price coercion (G4);
  row pruning on re-import (P1-5).
- **Exit:** revenue/seller numbers reconcile to a hand-computed truth table across re-imports.

### Phase 5 — Harden for unattended operation
- AI security: allowlist, `--strict-mcp-config`, drop bypass, injection guard (P0-2).
- Config (no hardcoded model/paths), dead-code purge, repo hygiene, docs (P3-*).
- **Exit:** survives a month unattended; a new teammate can run and extend it from the docs.

*(Phase 6 — scale — only if data grows 10×; indexes + pagination are pre-identified, not pre-built.)*

---

## 4. Key engineering decisions (ADR-style)

| Decision | Choice | Why / tradeoff |
|---|---|---|
| AI transport | **claude-CLI subscription**, never API key | Flat-fee; constraint is quota/latency not $. Lose: per-call leanness. Mitigate with `--model` + gating. |
| AI role | **Deterministic context → AI decision → code post-verify** | Bounds confabulation; gives a testable gate. The wrong-brand fix *is* this rule. |
| Unknown handling | **Explicit `unknown` state; qualify only on `verified`** | Eliminates the NULL-as-0 bug class. Cost: a "Candidate vs Verified" UX split (worth it). |
| Spend policy | **Revenue-gated + model-tiered** | Aligns cost with value; errors live in the cheap tail anyway. Cost: a few brands skipped (acceptable). |
| Determinism | **Re-verify/confirm before locking** borderline answers | Kills luck-of-the-run lock-in. Cost: an extra call on uncertain brands. |
| Stack | **Keep SQLite + Streamlit + launchd** | Right for the scale; rewriting is waste. Revisit only past ~10× data or multi-user. |
| Contact discovery | **Defer; design the seam now** | Highest product value but biggest scope; land the trustworthy list first. |

---

## 5. Risks & mitigations

- **AI nondeterminism / confabulation** → post-verify gate + confirmation pass + abstain-by-default.
- **Subscription quota exhaustion** → revenue-gate + tiering + budget ceiling + backoff.
- **Refactor-induced regressions** → tests-first (Phase 0); change one vertical slice at a time.
- **Scope creep into contact-discovery/CRM** → fenced behind Phase 3; seam designed, build deferred.
- **Single-operator bus factor** → docs (RUNBOOK + this plan) and CI keep it handoff-ready.

---

## 6. Success metrics (tracked, tied to the end goal)

| End-goal leg | Metric | Bar |
|---|---|---|
| Trustworthy | Verified-green precision (manual sample) | ≥ 95% |
| Trustworthy | Website wrong-brand rate | < 1% |
| Trustworthy | Ad-count accuracy vs ground truth | measured ≥ target |
| Affordable | Quota/latency per full refresh | ≪ today (Opus-on-all) |
| Prioritized | % of top-50 a rep would contact | high (rubric) |
| Actionable | Steps from a green row to a contact | small, with a path |
| Durable | CI green; unattended uptime | passing; 30-day |

---

## 7. Operating principles

- **Vertical slices, test-gated.** Every change ships behind a test; no phase starts before the prior
  one's exit criteria are green.
- **Truth before polish.** Never make the list prettier/faster while a green row can lie.
- **Measure, don't guess.** The audits set the precedent — decisions are backed by run data.
- **One judging question, always:** *does this make the green worklist something a rep can act on and
  close from?* If no, it waits.

**Immediate next step:** Phase 0's timeout/instrumentation, then **S2-1** — the smallest change that
moves the needle most, and the gate to everything in Phase 1.
