#!/usr/bin/env python3
"""Background brand-scan jobs for the dashboard.

The dashboard lets a user filter brands and queue a scan. That enqueues the
selected brand_keys into a job which runs in a DETACHED subprocess (so the
Streamlit UI never blocks). The job drives a small pool of single-brand scan
SUBPROCESSES — `brand_scan.py scan <key>` — **two at a time** (the requested
concurrency). One subprocess per brand keeps each scan fully isolated: its own
Chromium and its own SQLite connection, side-stepping Playwright/SQLite
threading hazards entirely.

Each brand_scan subprocess fills the brand's website/socials/fb-page + real
Meta & Google ad counts, but only for signals that are stale or missing (30-day
per-source freshness — see brand_scan.py). So re-queuing the same brands is
cheap: already-scanned signals are skipped.

Design notes
------------
* Job metadata lives in small JSON files under data/ad_jobs/<job_id>.json so the
  UI can poll progress without touching the SQLite write path. The RESULTS (ad
  counts, website) go into the brands table — that's the deliverable.
* Child stderr is inherited (-> the job .log), only the small JSON result line
  comes back over a stdout pipe, so the pool never deadlocks on a full pipe.

CLI:
    python3 ad_jobs.py run <job_id>     # executed by the detached subprocess
"""

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import config
import db
import green
import settings

JOBS_DIR = os.path.join(os.path.dirname(db.DB_PATH), "ad_jobs")
DEFAULT_CONCURRENCY = 2
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCAN = os.path.join(_HERE, "brand_scan.py")


def _scan_python():
    """Pick a Python interpreter that can actually import Playwright.

    `sys.executable` is NOT reliable here: when the dashboard runs under a venv
    whose `sys.executable` resolves to the *base* interpreter (a common venv
    quirk), spawning `[sys.executable, ...]` lands in an environment WITHOUT the
    venv's site-packages, so the scrapers fail with `No module named
    'playwright'`. We probe candidates and return the first that can import it.
    """
    env_py = os.environ.get("SCAN_PYTHON")
    candidates = [
        env_py,
        os.path.join(_HERE, ".venv", "bin", "python"),
        sys.executable,
        "/usr/bin/python3",
    ]
    for py in candidates:
        if not py or not os.path.exists(py):
            continue
        try:
            r = subprocess.run([py, "-c", "import playwright"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return sys.executable  # last resort; will surface a clear error if missing


_PY = _scan_python()


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _job_path(job_id):
    return os.path.join(JOBS_DIR, job_id + ".json")


def _write_job(job):
    os.makedirs(JOBS_DIR, exist_ok=True)
    tmp = _job_path(job["id"]) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(job, fh, indent=2)
    os.replace(tmp, _job_path(job["id"]))


def read_job(job_id):
    try:
        with open(_job_path(job_id)) as fh:
            return json.load(fh)
    except Exception:
        return None


def _is_dead(job, path):
    """A job worth pruning from the panel: fully-failed, or a 'running' zombie
    whose runner died (file untouched for >10 min, so it will never advance)."""
    res = job.get("results", [])
    total = job.get("total", 0) or 0
    errs = sum(1 for r in res
               if r.get("meta_error") or r.get("google_error")
               or r.get("status") == "error")
    if job.get("status") == "stopped":
        return True  # user-cancelled
    if job.get("status") == "done" and total and errs == total:
        return True  # every brand errored
    if job.get("status") in ("running", "queued"):
        try:
            stale = (time.time() - os.path.getmtime(path)) > 600
        except Exception:
            stale = False
        if stale:
            return True  # runner died; no progress for 10+ min
    return False


def prune_dead_jobs():
    """Delete fully-failed and zombie job files so the panel stays clean."""
    removed = []
    try:
        names = [f for f in os.listdir(JOBS_DIR) if f.endswith(".json")]
    except Exception:
        return removed
    for name in names:
        path = os.path.join(JOBS_DIR, name)
        try:
            with open(path) as fh:
                job = json.load(fh)
        except Exception:
            continue
        if _is_dead(job, path):
            for p in (path, path[:-5] + ".log"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            removed.append(job.get("id", name))
    return removed


def list_jobs(limit=10):
    """Most-recent jobs first (by file mtime). Self-cleans dead jobs first."""
    prune_dead_jobs()
    try:
        files = [os.path.join(JOBS_DIR, f) for f in os.listdir(JOBS_DIR)
                 if f.endswith(".json")]
    except Exception:
        return []
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            with open(f) as fh:
                out.append(json.load(fh))
        except Exception:
            continue
    return out


# ------------------------------------------------------------- job execution

def _parse_result(stdout, brand_key):
    """brand_scan prints its outcome as a single JSON line on stdout."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                break
    return {"brand_key": brand_key, "status": "error",
            "error": "no result emitted by brand_scan"}


CHILD_TIMEOUT = config._int("SCAN_CHILD_TIMEOUT", 300)  # kill a brand scan past this


def _expired(start_monotonic, now_monotonic, timeout=CHILD_TIMEOUT):
    """True if a child has exceeded its wall-clock deadline (pure → unit-tested)."""
    return (now_monotonic - start_monotonic) >= timeout


def _kill_tree(p):
    """SIGKILL a child and its whole process group (Chromium / claude grandchildren)."""
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def scan_signal(brand_key, signal, timeout=200):
    """Synchronously refresh ONE signal (website|meta|google) for one brand in an
    isolated subprocess (own Chromium/AI). Returns the outcome dict. Used by the
    dashboard's per-field refresh buttons; not gated by the daily cap."""
    cmd = [_PY, _SCAN, "scan", brand_key, "--only", signal]
    try:
        p = subprocess.run(cmd, cwd=_HERE, capture_output=True, text=True,
                           timeout=timeout)
    except Exception as e:
        return {"brand_key": brand_key, "status": "error", "error": str(e)}
    return _parse_result(p.stdout, brand_key)


def interrupted_jobs():
    """Jobs still marked running/queued — candidates for cleanup when no worker is
    alive (i.e. the worker died mid-job). The dashboard pairs this with the
    heartbeat to tell the user exactly what to clear."""
    return [j for j in list_jobs(limit=200) if j.get("status") in ("running", "queued")]


def run_job(job_id, concurrency=None, progress_cb=None):
    """Execute a queued scan job with a pool of `concurrency` brand_scan
    subprocesses. Each child has a wall-clock deadline (CHILD_TIMEOUT) so one hung
    scan can never freeze the worker. `progress_cb(job, inflight_keys)` is called
    on every state change (and periodically) so the worker can publish a heartbeat."""
    job = read_job(job_id)
    if not job:
        print(f"job {job_id} not found", file=sys.stderr)
        return
    concurrency = concurrency or job.get("concurrency") or DEFAULT_CONCURRENCY
    force = bool(job.get("force"))
    job["status"] = "running"
    job["started_at"] = _now()
    _write_job(job)

    running = {}    # Popen -> brand_key
    started = {}    # Popen -> monotonic start time

    def _tick():
        if progress_cb:
            try:
                progress_cb(job, [running[p] for p in running])
            except Exception:
                pass

    def _record(outcome, label):
        job["results"].append(outcome)
        job["done"] = len(job["results"])
        _write_job(job)
        _tick()
        print(f"  [{job['done']}/{job['total']}] {label}", file=sys.stderr)

    keys = list(job["brand_keys"])
    idx = 0
    capped = False
    _tick()
    while idx < len(keys) or running:
        # top up the pool — but never exceed the LIVE daily cap. settings.can_scan()
        # re-reads the cap + today's count every iteration, so lowering the cap
        # mid-run stops new launches immediately.
        while (len(running) < concurrency and idx < len(keys)
               and settings.can_scan()):
            k = keys[idx]
            idx += 1
            cmd = [_PY, _SCAN, "scan", k] + (["--force"] if force else [])
            p = subprocess.Popen(cmd, cwd=_HERE, stdout=subprocess.PIPE, text=True,
                                 start_new_session=True)  # own group → killable subtree
            running[p] = k
            started[p] = time.monotonic()
            _tick()
        # Daily cap reached with nothing left to harvest -> stop; the rest waits
        # for tomorrow (or for the cap to be raised).
        if not running and idx < len(keys) and not settings.can_scan():
            capped = True
            break
        # enforce per-child deadline
        now = time.monotonic()
        for p in [p for p in list(running) if _expired(started[p], now)]:
            k = running.pop(p)
            started.pop(p, None)
            _kill_tree(p)
            settings.record_scan(k)     # a timeout still consumed a scan slot
            _record({"brand_key": k, "status": "timeout",
                     "error": f"killed after {CHILD_TIMEOUT}s"}, f"{k}: TIMEOUT")
        # harvest finished
        finished = [p for p in list(running) if p.poll() is not None]
        if not finished:
            _tick()                     # keep the heartbeat fresh during long scans
            time.sleep(0.4)
            continue
        for p in finished:
            k = running.pop(p)
            started.pop(p, None)
            stdout, _ = p.communicate()
            outcome = _parse_result(stdout, k)
            if outcome.get("did"):      # count only brands that did real work today
                settings.record_scan(k)
            _record(outcome, f"{outcome.get('brand', k)}: {outcome.get('status')} "
                             f"did={outcome.get('did')}")

    # Refresh the green pitch-list so the Green Prospects page reflects new counts.
    try:
        conn = db.connect()
        db.init_db(conn)
        green.recompute_all(conn)
        green.export_csv(conn)
    except Exception as e:
        print(f"  green refresh failed: {e}", file=sys.stderr)

    job["status"] = "done"
    job["finished_at"] = _now()
    if capped:
        job["capped"] = True
        job["note"] = (f"Stopped at the daily cap ({settings.get_daily_cap()}); "
                       f"{len(keys) - job['done']} brand(s) left for the next day.")
    _write_job(job)
    _tick()


def enqueue(brand_keys, force=False, brand_names=None, detached=True,
            concurrency=DEFAULT_CONCURRENCY, progress_cb=None):
    """Create a scan job for `brand_keys` and launch it in a detached subprocess.
    Returns the job dict. Already-fresh signals are skipped unless `force`.
    `progress_cb` is forwarded to run_job for the synchronous (worker) path."""
    brand_keys = [k for k in dict.fromkeys(brand_keys) if k]  # dedupe, keep order
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": "scan",
        "brand_keys": brand_keys,
        "brand_names": list(brand_names or [])[:len(brand_keys)],
        "force": bool(force),
        "concurrency": int(concurrency),
        "total": len(brand_keys),
        "done": 0,
        "results": [],
        "status": "queued",
        "created_at": _now(),
    }
    _write_job(job)
    if detached and brand_keys:
        log = os.path.join(JOBS_DIR, job["id"] + ".log")
        with open(log, "w") as fh:
            subprocess.Popen(
                [_PY, os.path.abspath(__file__), "run", job["id"]],
                cwd=_HERE, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True)  # detach: survives Streamlit reruns
    elif brand_keys:
        run_job(job["id"], progress_cb=progress_cb)
        job = read_job(job["id"])
    return job


# ------------------------------------------------------ live process controls

def _pgrep(pattern):
    """PIDs of processes whose command line matches `pattern` (excluding us)."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return [int(x) for x in out.stdout.split() if int(x) != os.getpid()]
    except Exception:
        return []


def runner_pids():
    """Detached dashboard scan-job runners (`ad_jobs.py run <id>`) currently alive."""
    return _pgrep("ad_jobs.py run")


def is_any_running():
    """True if a dashboard-launched scan job is currently running. Used to stop
    the UI from starting a second job in parallel."""
    return bool(runner_pids())


def worker_running():
    """True if the always-on background worker (auto.py) is alive."""
    return bool(_pgrep("amazon-prospect-tool/auto.py")) or bool(_pgrep("/auto.py"))


def stop_running():
    """Terminate detached scan-job runners (and their brand_scan children via the
    process group) and mark any running/queued job files as 'stopped'. Does NOT
    touch the background worker — manage that with ./manage.sh."""
    killed = []
    for pid in runner_pids():
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)  # runner + its scan children
            killed.append(pid)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except Exception:
                pass
    stopped = []
    try:
        names = [f for f in os.listdir(JOBS_DIR) if f.endswith(".json")]
    except Exception:
        names = []
    for name in names:
        path = os.path.join(JOBS_DIR, name)
        try:
            with open(path) as fh:
                job = json.load(fh)
        except Exception:
            continue
        if job.get("status") in ("queued", "running"):
            job["status"] = "stopped"
            job["finished_at"] = _now()
            _write_job(job)
            stopped.append(job.get("id"))
    return {"killed": killed, "stopped": stopped}


def clear_all_jobs():
    """Delete every job file (progress records only — brand data lives in the
    brands table and is untouched). Returns the number of jobs removed."""
    n = 0
    try:
        names = os.listdir(JOBS_DIR)
    except Exception:
        return 0
    for name in names:
        if name.endswith((".json", ".log")):
            try:
                os.remove(os.path.join(JOBS_DIR, name))
                n += name.endswith(".json")
            except OSError:
                pass
    return n


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run" and len(sys.argv) >= 3:
        run_job(sys.argv[2])
    elif cmd == "prune":                       # delete failed + zombie jobs
        print("pruned:", prune_dead_jobs())
    elif cmd == "clear":                        # delete ALL job records
        print(f"cleared {clear_all_jobs()} job(s)")
    elif cmd == "list":                         # show recent jobs
        for j in list_jobs(limit=20):
            print(f"{j['id']}  {j.get('status'):8} "
                  f"{j.get('done')}/{j.get('total')}")
    else:
        print("usage: python3 ad_jobs.py {run <job_id>|prune|clear|list}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
