# Amazon Prospect Finder

Find **green prospects** — brands with proven Amazon revenue but little or no
direct-to-consumer presence (no website, few/no Meta or Google ads). Those are
the brands worth pitching.

It's a **Streamlit dashboard** over a local SQLite database, plus an **always-on
background worker** that scans each brand for its website + real Meta & Google ad
counts and surfaces direct, outreach-ready links.

> **Philosophy — navigation, not judgment.** The tool hands you links and counts;
> you make the call. When a scraper can't trust a count it records *unknown*
> (`None`), **never zero** — so a brand is never falsely marked "green". Meta links
> are direct **page** links (`view_all_page_id=…`), Google links are **domain**
> transparency links (`?domain=…`); brand-name keyword-search links are never shown.

---

## Quickstart

```bash
cd /Users/rabiulislam/Desktop/Folders/amazon-prospect-tool

# 1. Start the dashboard (always use the venv — it has Playwright + the scrapers)
.venv/bin/streamlit run dashboard.py
#   …or headless on a fixed port:
nohup .venv/bin/streamlit run dashboard.py --server.headless true \
  --server.port 8501 > data/dashboard.log 2>&1 &
open http://localhost:8501

# 2. Start the always-on background scanner (launchd; survives reboots)
./manage.sh start
```

Then drop a Helium10 Black Box export (CSV/XLSX) into `~/Downloads` — the worker
ingests it (de-duping on ASIN), builds the brand/seller tables, and scans new or
stale brands two at a time.

---

## What you get

- **Dashboard** (`dashboard.py`) — one unified search box (brand / product / ASIN /
  category / seller) or an advanced filter, with results in **Brands / Products /
  Sellers** tabs. A sidebar control panel manages scan jobs and cache.
- **Green Prospects** page (`pages/1_Green_Prospects.py`) — the pitch list, served
  live from the brands table. Filter by website, Meta/Google ad ceilings, revenue
  and reviews; every row carries a Meta page link, a Google domain link, and a
  **Scanned** flag so reps know which counts are verified.
- **Background worker** (`auto.py`) — ingests, then scans stale/new brands at
  concurrency 2. A signal (website / Meta / Google) is only re-scanned if it's
  missing or older than 30 days.

---

## How a brand gets scanned

`brand_scan.py scan <brand_key>` resolves, for each stale signal only:

1. **Website / socials / Facebook page id** — deterministic search, accepted only
   if the domain matches the brand (else abstains).
2. **Meta ad count** — via `meta_ad_scraper` (reuses the resolved FB page id).
3. **Google ad count** — via `google_ad_scraper` (reuses the resolved domain).

Counts that can't be trusted are stored as `None` (unknown), never 0. Each signal
stamps its own `*_scanned_at` timestamp to drive the 30-day freshness rule.

Jobs run as a concurrency-2 pool of **one subprocess per brand** (`ad_jobs.py`),
so every scan is fully isolated — its own Chromium, its own SQLite connection.

---

## Operating it

### Dashboard sidebar controls
Start a scan (guarded so two never run in parallel) · Stop running job · Clear
abandoned job data · Clear ALL job records · Clear cache. The panel also lists the
terminal helper commands.

### Background worker (`./manage.sh`)
```bash
./manage.sh start | stop | restart | status | log | run
```

### Scan jobs (CLI)
```bash
.venv/bin/python ad_jobs.py list     # show recent jobs
.venv/bin/python ad_jobs.py prune    # delete failed + stopped + zombie jobs
.venv/bin/python ad_jobs.py clear    # delete ALL job records
.venv/bin/python brand_scan.py scan <brand_key> [--force]   # one brand
```
Job files in `data/ad_jobs/` are progress records only — real results live in the
`brands` table, so deleting them never loses data.

**Full operational reference: [`RUNBOOK.md`](RUNBOOK.md).**

---

## Golden rule: use the venv Python

Always launch with `.venv/bin/python` / `.venv/bin/streamlit`. The venv has
Playwright and the scrapers; the system / Homebrew base Python does not. Spawning
scans from the wrong interpreter is the #1 failure (`No module named 'playwright'`,
every brand errors). `ad_jobs.py` self-protects by resolving a Playwright-capable
interpreter for scan subprocesses, but launch the UI/worker from the venv anyway.

## Files

| File | Role |
|---|---|
| `dashboard.py` | Streamlit search UI + sidebar controls |
| `pages/1_Green_Prospects.py` | Green pitch-list page |
| `auto.py` | Always-on ingest + scan worker |
| `brand_scan.py` | Per-brand scanner (website + Meta + Google) |
| `ad_jobs.py` | Concurrency-2 scan-job engine + job CLI |
| `query.py` | Unified search + advanced filter |
| `pipeline.py` | CSV/XLSX ingest, brand/seller build |
| `green.py` | Green rule, flagging, CSV export |
| `db.py` | SQLite schema, WAL, migrations |
| `meta_ad_scraper.py` / `google_ad_scraper.py` | Ad-count scrapers (don't modify) |
| `manage.sh` | launchd control for `auto.py` |

## Secrets

Facebook token and paid-API keys (`SCRAPECREATORS_API_KEY`, `SERPAPI_KEY`) live in
gitignored `secrets.json`. Never commit or print them.
