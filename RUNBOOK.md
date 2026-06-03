# Runbook — Amazon Prospect Tool

Operational commands for running the dashboard, the background scanner, and
managing scan jobs. **Always use the project venv (`.venv/bin/...`)** — it has
Playwright + the scrapers. Using the wrong Python is the #1 cause of
`No module named 'playwright'` errors.

> All commands assume you are in the project root:
> `cd /Users/rabiulislam/Desktop/Folders/amazon-prospect-tool`

---

## 1. Streamlit dashboard

The UI: unified search, three result tabs (Brands / Products / Sellers), and the
**Green Prospects** page.

```bash
# Start (foreground)
.venv/bin/streamlit run dashboard.py

# Start (background, fixed port, no browser auto-open)
nohup .venv/bin/streamlit run dashboard.py \
  --server.headless true --server.port 8501 > data/dashboard.log 2>&1 &

# Open it
open http://localhost:8501

# Stop ALL dashboard instances
pkill -f "streamlit run dashboard.py"

# Tail its log
tail -f data/dashboard.log
```

**After editing any `.py` file, restart the dashboard** — Streamlit caches
imported modules in memory, so code changes (e.g. to `ad_jobs.py`) are not picked
up until restart. Symptom of a stale dashboard: jobs keep failing or behave like
old code.

---

## 2. Background scanner (auto worker)

`auto.py` runs forever: ingests new Helium10 exports from `~/Downloads`, then
scans stale/new brands for website + Meta + Google ad counts, **two at a time**.
A brand signal is only re-scanned if missing or older than 30 days. Managed as a
**launchd** agent so it survives crashes and reboots.

```bash
./manage.sh start      # install + start (RunAtLoad + KeepAlive)
./manage.sh stop       # stop + uninstall
./manage.sh restart    # stop then start (use after editing auto.py / ad_jobs.py)
./manage.sh status     # is it running?
./manage.sh log        # tail the worker log (data/auto.log)
./manage.sh run        # run in the FOREGROUND instead (Ctrl-C to quit)
```

Quick health checks:

```bash
# Should be a single auto.py process, on the .venv python
ps -o pid,command -ax | grep "amazon-prospect-tool/auto.py" | grep -v grep

# Should print 0
grep -c "No module named 'playwright'" data/auto.log

# Scan progress (how many brands have a Meta scan so far)
.venv/bin/python -c "import db;c=db.connect();print(c.execute(\"SELECT COUNT(*) FROM brands WHERE meta_scanned_at IS NOT NULL\").fetchone()[0])"
```

### Manually queue / run a scan (without the worker)

```bash
# Scan one brand by key (re-scans only stale signals; --force does all)
.venv/bin/python brand_scan.py scan <brand_key> [--force]

# Run a queued job id synchronously (normally the detached runner does this)
.venv/bin/python ad_jobs.py run <job_id>
```

---

## 3. Scan jobs — list & delete

Scan jobs are small JSON progress files under `data/ad_jobs/`. **They are just
progress records** — the real results live in the `brands` table, so deleting job
files never loses data.

```bash
# List recent jobs (id / status / done-total)
.venv/bin/python ad_jobs.py list

# Delete only FAILED + ZOMBIE jobs
#   - failed = every brand errored
#   - zombie = "running" but the runner died (no progress for 10+ min)
.venv/bin/python ad_jobs.py prune

# Delete ALL job records (clean slate)
.venv/bin/python ad_jobs.py clear

# Raw cleanup (equivalent to clear)
rm -f data/ad_jobs/*.json data/ad_jobs/*.log
```

> The dashboard's **Scan jobs** panel auto-prunes failed/zombie jobs every time
> it loads (via `list_jobs`), so you rarely need `prune` by hand — but it's there
> for the CLI. If failed jobs keep reappearing in the UI, the dashboard is
> running stale code → **restart it** (section 1).

---

## 4. Data maintenance

```bash
# Recompute green flags + rewrite data/green_prospects.csv from the brands table
.venv/bin/python -c "import db,green;c=db.connect();green.recompute_all(c);green.export_csv(c);print('done')"

# Purge any junk brand-name keyword-search Meta URLs (should be none)
.venv/bin/python -c "import db;c=db.connect();c.execute(\"UPDATE brands SET meta_ads_url='' WHERE meta_ads_url LIKE '%keyword_unordered%'\");c.commit();print('purged')"
```

Link formats the dashboard shows (never a brand-name text search):
- **Meta** → `…/ads/library/?…&search_type=page&view_all_page_id=<fb_page_id>`
- **Google** → `https://adstransparency.google.com/?region=anywhere&domain=<domain>`

---

## 5. Typical day-to-day

```bash
./manage.sh start                       # ensure the scanner is running
nohup .venv/bin/streamlit run dashboard.py --server.headless true \
  --server.port 8501 > data/dashboard.log 2>&1 &   # bring up the UI
open http://localhost:8501
# drop Helium10 CSV/XLSX exports into ~/Downloads — the worker ingests + scans them
```

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named 'playwright'` in jobs | Spawned by a Python without Playwright | Run dashboard/worker from `.venv`; restart both |
| Failed jobs keep showing in UI | Dashboard holding stale code in memory | `pkill -f "streamlit run dashboard.py"` then restart |
| Job stuck "running" forever | Runner process died (zombie) | `.venv/bin/python ad_jobs.py prune` |
| `database is locked` | Concurrent writers | WAL is enabled; retry. Don't run two workers at once |
| Worker not picking up a CSV | File not in `~/Downloads` or already ingested | Check `data/auto.log`; ASINs are de-duped on import |
