"""Shared sidebar control panel, rendered on every page.

Streamlit renders the sidebar per-page, so this lives in one module and is called
from both dashboard.py and pages/1_Green_Prospects.py. It manages scan jobs
(start — guarded against parallel runs — stop, clear) and busts the caller's data
cache via the supplied `bust_caches` callback.
"""

import streamlit as st

import ad_jobs
import brand_scan


def render_sidebar(get_conn, bust_caches=lambda: None):
    """Render the controls. `get_conn` returns a SQLite connection (row factory),
    `bust_caches` clears the calling page's @st.cache_data tables."""
    with st.sidebar:
        st.markdown("### ⚙️ Controls")

        job_running = ad_jobs.is_any_running()
        worker_on = ad_jobs.worker_running()
        st.markdown(
            f"**Scan job:** {'🟢 running' if job_running else '⚪ idle'}  \n"
            f"**Background worker:** {'🟢 on' if worker_on else '⚪ off'}")
        st.divider()

        # --- start a new scan, but never in parallel with another job ---
        if st.button("▶️  Start scan (stale brands)", use_container_width=True,
                     type="primary", disabled=job_running):
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
            st.caption("A scan job is already running. Stop it before starting "
                       "another to avoid parallel scans.")
        elif worker_on:
            st.caption("The background worker is already scanning continuously; a "
                       "manual scan is usually unnecessary (30-day freshness dedupes).")

        # --- stop / clean ---
        if st.button("⏹️  Stop running job", use_container_width=True,
                     disabled=not job_running):
            res = ad_jobs.stop_running()
            st.toast(f"Stopped {len(res['stopped'])} job(s), "
                     f"killed {len(res['killed'])} process(es).")
            st.rerun()

        if st.button("🧹  Clear abandoned job data", use_container_width=True):
            removed = ad_jobs.prune_dead_jobs()
            st.toast(f"Pruned {len(removed)} failed/stopped/zombie job(s).")
            st.rerun()

        if st.button("🗑️  Clear ALL job records", use_container_width=True):
            n = ad_jobs.clear_all_jobs()
            st.session_state["scan_jobs"] = []
            st.toast(f"Cleared {n} job record(s).")
            st.rerun()

        if st.button("♻️  Clear cache & reload", use_container_width=True):
            bust_caches()
            st.toast("Cache cleared.")
            st.rerun()

        st.divider()
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
