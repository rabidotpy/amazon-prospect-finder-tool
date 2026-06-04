"""Shared sidebar control panel, rendered on every page.

Streamlit renders the sidebar per-page, so this lives in one module and is called
from both dashboard.py and pages/1_Green_Prospects.py.

It is the worker's window to the user: it always shows whether the background
worker is running, what brand/job it is processing right now, and — if the worker
died mid-job — exactly what to start and what to clean up. No silent failures.
"""

import os
import subprocess

import streamlit as st

import ad_jobs
import brand_scan
import settings
import worker_status

_HERE = os.path.dirname(os.path.abspath(__file__))


def _manage(action):
    """Run ./manage.sh <action> (start/stop/restart) for the launchd worker."""
    try:
        r = subprocess.run(["bash", os.path.join(_HERE, "manage.sh"), action],
                           cwd=_HERE, capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _worker_panel():
    """Always-visible worker status: running/idle/stopped + current activity, with
    a one-click start and interrupted-job cleanup when it's down."""
    st.markdown("### 🛰️ Background worker")
    s = worker_status.summary()
    # Heartbeat is the source of truth; fall back to a process check for the very
    # first run before any heartbeat exists.
    alive = s["alive"] or (s["state"] == "unknown" and ad_jobs.worker_running())

    if alive and s["state"] == "scanning":
        st.success(f"🟢 Scanning — **{s.get('current') or '…'}**")
        total = s.get("total") or 0
        if total:
            done = s.get("done") or 0
            st.progress(min(done / max(total, 1), 1.0),
                        text=f"job {s.get('job_id') or '—'} · {done}/{total}")
        if s.get("age") is not None:
            st.caption(f"updated {int(s['age'])}s ago")
    elif alive:
        st.success("🟢 Worker running — idle, watching ~/Downloads")
        if s.get("age") is not None:
            st.caption(f"last heartbeat {int(s['age'])}s ago")
    else:
        st.error("🔴 Worker is **not running** — new brands won't be processed.")
        if s.get("heartbeat"):
            st.caption(f"last seen: {s['heartbeat']}")
        if s.get("last_error"):
            st.caption(f"last error: {s['last_error']}")
        if st.button("▶️  Start background worker", type="primary",
                     use_container_width=True):
            ok, msg = _manage("start")
            st.toast("Worker starting…" if ok else f"Start failed: {msg[:80]}")
            st.rerun()
        st.caption("…or in a terminal: `./manage.sh start`")

    # Daily scan budget (live cap).
    done_today, cap = settings.scanned_today(), settings.get_daily_cap()
    st.caption(f"📊 Scanned today: **{done_today} / {cap}**"
               + ("  · cap reached" if done_today >= cap else
                  f"  · {cap - done_today} left"))

    # Interrupted jobs: worker died mid-job, leaving 'running'/'queued' job files.
    interrupted = ad_jobs.interrupted_jobs()
    if interrupted and not (alive and s["state"] == "scanning"):
        st.warning(f"⚠️ {len(interrupted)} job(s) were left mid-run when the worker "
                   f"stopped — their data is stale.")
        if st.button(f"🧹  Clear {len(interrupted)} interrupted job(s)",
                     use_container_width=True):
            ad_jobs.stop_running()
            ad_jobs.prune_dead_jobs()
            st.toast("Cleared interrupted jobs.")
            st.rerun()


def render_sidebar(get_conn, bust_caches=lambda: None):
    """Render the controls. `get_conn` returns a SQLite connection (row factory),
    `bust_caches` clears the calling page's @st.cache_data tables."""
    with st.sidebar:
        _worker_panel()
        st.divider()

        st.markdown("### 🔎 Manual scan")
        job_running = ad_jobs.is_any_running()

        if st.button("▶️  Scan stale brands now", use_container_width=True,
                     disabled=job_running):
            keys = brand_scan.stale_brand_keys(get_conn(), limit=200)
            if not keys:
                st.toast("Nothing stale to scan — all brands fresh.")
            else:
                job = ad_jobs.enqueue(keys)
                st.session_state["scan_jobs"] = [job["id"]] + \
                    st.session_state.get("scan_jobs", [])
                st.toast(f"Started scan for {job['total']} brand(s).")
                st.rerun()
        if job_running:
            st.caption("A manual scan job is already running.")

        if st.button("⏹️  Stop running job", use_container_width=True,
                     disabled=not job_running):
            res = ad_jobs.stop_running()
            st.toast(f"Stopped {len(res['stopped'])} job(s), "
                     f"killed {len(res['killed'])} process(es).")
            st.rerun()

        with st.expander("⚙️  Settings"):
            cur = settings.get_daily_cap()
            new = st.number_input(
                "Daily scan cap (brands/day)", min_value=0, max_value=100000,
                value=cur, step=10,
                help="Max brands scanned per day across the worker and manual scans. "
                     "Applied LIVE — lowering it stops a running job once today's "
                     "count reaches the new value. Resets at midnight.")
            if int(new) != cur:
                settings.set_daily_cap(int(new))
                st.toast(f"Daily cap set to {int(new)}.")
                st.rerun()
            st.caption(f"Scanned today: {settings.scanned_today()} / {settings.get_daily_cap()}")

        with st.expander("🧹  Maintenance"):
            if st.button("Clear abandoned job data", use_container_width=True):
                removed = ad_jobs.prune_dead_jobs()
                st.toast(f"Pruned {len(removed)} failed/stopped/zombie job(s).")
                st.rerun()
            if st.button("Clear ALL job records", use_container_width=True):
                n = ad_jobs.clear_all_jobs()
                st.session_state["scan_jobs"] = []
                st.toast(f"Cleared {n} job record(s).")
                st.rerun()
            if st.button("Clear cache & reload", use_container_width=True):
                bust_caches()
                st.toast("Cache cleared.")
                st.rerun()

        with st.expander("⌨️  Terminal helper commands"):
            st.code(
                "# dashboard\n"
                ".venv/bin/streamlit run dashboard.py\n\n"
                "# background worker (always-on scanner)\n"
                "./manage.sh start | stop | restart | status | log\n\n"
                "# scan jobs\n"
                ".venv/bin/python ad_jobs.py list     # show jobs\n"
                ".venv/bin/python ad_jobs.py prune    # delete failed+zombie\n"
                ".venv/bin/python ad_jobs.py clear    # delete ALL job records\n\n"
                "# manual single-brand scan\n"
                ".venv/bin/python brand_scan.py scan <brand_key> [--force]",
                language="bash")
        st.caption("Full reference: RUNBOOK.md")
