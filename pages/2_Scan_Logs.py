"""Scan Logs — a live, terminal-style view of the scan pipeline.

Shows, as each brand is scanned: the phase, the brand, every AI call (model,
duration, prompt preview, result, cost), and the total time per brand. Auto-
refreshes while the worker is scanning.
"""
import time

import pandas as pd
import streamlit as st

import ad_jobs
import controls
import db
import scan_log
import settings
import worker_status

st.set_page_config(page_title="Scan Logs", page_icon="🖥️", layout="wide")
st.markdown("<style>#MainMenu,footer{visibility:hidden;}"
            ".block-container{padding-top:2.2rem;max-width:1500px;}</style>",
            unsafe_allow_html=True)
st.markdown(scan_log.TERM_CSS, unsafe_allow_html=True)

controls.render_sidebar(lambda: db.connect(), lambda: None)

st.title("🖥️ Scan Logs")

s = worker_status.summary()
c = st.columns([2, 1, 1])
if s["alive"] and s["state"] == "scanning":
    c[0].success(f"🟢 Scanning — {s.get('current') or '…'}")
elif s["alive"]:
    c[0].info("🟢 Worker idle")
else:
    c[0].error("🔴 Worker not running")
auto = c[1].toggle("Auto-refresh", value=(s["alive"] and s["state"] == "scanning"))
if c[2].button("🧹 Clear logs"):
    scan_log.clear()
    st.rerun()
st.caption(f"Scanned today: {settings.scanned_today()} / {settings.get_daily_cap()} "
           f"(daily cap) · the worker scans the highest-revenue brands with stale "
           f"website / Meta / Google signals, 2 at a time.")

events = scan_log.tail(400)
if not events:
    st.info("No scan activity yet. Logs appear here live once the worker (or a "
            "manual scan / refresh) starts scanning brands.")
else:
    st.markdown(f"<div class='term' style='height:560px'>"
                f"{scan_log.format_html(events)}</div>", unsafe_allow_html=True)
    st.caption(f"{len(events)} recent events · newest first")

# ----------------------------------------------------- worker job batches
st.divider()
st.markdown("### 📦 Scan jobs (batches)")
st.caption("Each batch is one run of the pool (worker cycles + any manual scans). "
           "Newest first.")
jobs = ad_jobs.list_jobs(limit=8)
if not jobs:
    st.info("No scan jobs yet.")
for j in jobs:
    done, total = j.get("done", 0), j.get("total", 0)
    status = j.get("status", "?")
    icon = {"running": "🟢", "queued": "⏳", "done": "✓", "stopped": "⏹️"}.get(status, "•")
    label = f"{icon} {status} · {done}/{total} · {j.get('id', '')}"
    if j.get("capped"):
        label += "  · capped"
    with st.expander(label, expanded=(status in ("running", "queued"))):
        if total:
            st.progress(min(done / max(total, 1), 1.0))
        if j.get("note"):
            st.caption(j["note"])
        if j.get("results"):
            df = pd.DataFrame(j["results"])
            keep = [c for c in ["brand", "status", "did", "website_url",
                                "meta_count", "google_count"] if c in df.columns]
            st.dataframe(df[keep] if keep else df, use_container_width=True,
                         hide_index=True, height=min(320, 60 + 32 * len(df)))

if auto and s["alive"] and s["state"] == "scanning":
    time.sleep(2.5)
    st.rerun()
