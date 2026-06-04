"""Scan Logs — a live, terminal-style view of the scan pipeline.

Shows, as each brand is scanned: the phase, the brand, every AI call (model,
duration, prompt preview, result, cost), and the total time per brand. Auto-
refreshes while the worker is scanning.
"""
import time

import streamlit as st

import controls
import db
import scan_log
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

events = scan_log.tail(400)
if not events:
    st.info("No scan activity yet. Logs appear here live once the worker (or a "
            "manual scan / refresh) starts scanning brands.")
else:
    st.markdown(f"<div class='term' style='height:560px'>"
                f"{scan_log.format_html(events)}</div>", unsafe_allow_html=True)
    st.caption(f"{len(events)} recent events · newest first")

if auto and s["alive"] and s["state"] == "scanning":
    time.sleep(2.5)
    st.rerun()
