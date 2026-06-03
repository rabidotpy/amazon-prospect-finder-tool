#!/usr/bin/env python3
"""Always-on background worker for the Amazon prospecting tool.

It needs no one to drive it. In a loop it:
  1. INGESTS any new Helium10 export dropped in ~/Downloads -> products table.
  2. SYNCS the brand skeleton (new brands enter as `pending`, enriched_at=NULL).
  3. ENRICHES pending brands ONE AT A TIME (website -> socials -> page_id ->
     Meta/Google ad counts), pacing politely between brands.
  4. After each brand, recomputes its GREEN flag and rewrites
     data/green_prospects.csv, so the dashboard's Green page is always current.
  5. When nothing is pending, it idles (POLL_SECONDS) and re-checks Downloads.

Run in the foreground:   python3 auto.py
Run unattended:          ./manage.sh start     (launchd; see manage.sh)
Logs go to stderr (launchd redirects them to data/auto.log).
"""

import signal
import sys
import time
from datetime import datetime

import ad_jobs
import brand_scan
import config
import db
import green
import pipeline

_STOP = False
# How many stale brands to scan per cycle before re-checking Downloads. Keeps
# job sizes bounded so freshly-dropped exports get picked up promptly.
SCAN_BATCH = config._int("SCAN_BATCH", 50)
SCAN_CONCURRENCY = config._int("SCAN_CONCURRENCY", 2)


def _stop(*_):
    global _STOP
    _STOP = True
    log("stop signal received; finishing current brand then exiting")


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr, flush=True)


def cycle(conn):
    """One full pass: ingest any new export, then scan a batch of stale brands at
    SCAN_CONCURRENCY (= 2) via the brand_scan subprocess pool. Returns the number
    of brands scanned this pass."""
    nfiles, nrows = pipeline.ingest_new(conn)
    if nfiles:
        log(f"ingested {nrows} rows from {nfiles} new file(s)")
        pipeline.sync_brand_skeleton(conn)
        pipeline.rebuild_sellers(conn)

    keys = brand_scan.stale_brand_keys(conn, limit=SCAN_BATCH)
    if not keys:
        green.export_csv(conn)
        return 0

    log(f"{len(keys)} stale/new brand(s) -> scanning (concurrency={SCAN_CONCURRENCY})")
    # Run the pool synchronously in this worker (not detached); it scans only the
    # signals that are stale/missing and refreshes the green CSV when done.
    job = ad_jobs.enqueue(keys, detached=False, concurrency=SCAN_CONCURRENCY)
    greens = conn.execute("SELECT COUNT(*) c FROM brands WHERE is_green=1").fetchone()["c"]
    log(f"  scanned {job.get('done', 0)} brand(s); {greens} green so far")
    return job.get("done", 0)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    conn = db.connect()
    db.init_db(conn)

    log(f"auto worker started (GREEN_MAX_ADS={config.GREEN_MAX_ADS}, "
        f"poll={config.POLL_SECONDS}s, batch={SCAN_BATCH}, "
        f"concurrency={SCAN_CONCURRENCY})")
    green.recompute_all(conn)
    green.export_csv(conn)

    while not _STOP:
        try:
            did = cycle(conn)
        except Exception as e:
            log(f"cycle error: {e}")
            did = 0
        if _STOP:
            break
        if did == 0:
            # idle: wait for new downloads, but stay responsive to signals
            for _ in range(config.POLL_SECONDS):
                if _STOP:
                    break
                time.sleep(1)

    log("auto worker stopped")


if __name__ == "__main__":
    main()
