"""Scan Logs — a live, terminal-style view of the scan pipeline.

Shows, as each brand is scanned: the phase, the brand, every AI call (model,
duration, prompt preview, result, cost), and the total time per brand. Auto-
refreshes while the worker is scanning.
"""
import html
import time

import streamlit as st

import controls
import db
import scan_log
import worker_status

st.set_page_config(page_title="Scan Logs", page_icon="🖥️", layout="wide")
st.markdown(
    """
    <style>
      #MainMenu, footer {visibility: hidden;}
      .block-container {padding-top: 2.2rem; max-width: 1500px;}
      .term {background:#0b1020; color:#d7e0f5; border-radius:10px; padding:14px 16px;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size:12.5px; line-height:1.55; height:560px; overflow-y:auto;
             white-space:pre-wrap; border:1px solid #1e2a44;}
      .term .t  {color:#5b6b8c;}
      .term .b  {color:#7ee787; font-weight:600;}
      .term .ai {color:#79c0ff;}
      .term .ok {color:#7ee787;}
      .term .er {color:#ff7b72;}
      .term .ph {color:#e3b341;}
      .term .dim{color:#8b98b8;}
    </style>
    """,
    unsafe_allow_html=True,
)

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


def esc(x):
    return html.escape(str(x))


def render(ev):
    ts = f"<span class='t'>{esc(ev.get('ts','')[11:])}</span> "
    k = ev.get("kind")
    if k == "brand_start":
        sigs = ", ".join(ev.get("signals", []))
        return (ts + f"<span class='b'>▶ START</span> {esc(ev.get('brand'))} "
                f"<span class='dim'>· ${ev.get('revenue') or 0:,.0f} · "
                f"{ev.get('mode')} · [{esc(sigs)}]</span>")
    if k == "phase":
        sig = ev.get("signal", "")
        if ev.get("error"):
            body = f"<span class='er'>error: {esc(ev['error'])}</span>"
        elif ev.get("state"):
            body = f"<span class='dim'>{esc(ev['state'])}</span>"
        else:
            d = f" <span class='dim'>({ev.get('duration_s')}s)</span>" if ev.get("duration_s") is not None else ""
            body = f"{esc(ev.get('result',''))}{d}"
        return ts + f"  <span class='ph'>• {esc(sig)}</span> {body}"
    if k == "ai_call":
        ok = "ok" if ev.get("ok") else "er"
        cost = f" · ${ev['cost_usd']:.3f}" if ev.get("cost_usd") else ""
        wsr = f" · web×{ev['web_searches']}" if ev.get("web_searches") else ""
        head = (ts + f"  <span class='ai'>🤖 {esc(ev.get('purpose'))}</span> "
                f"<span class='dim'>{esc(ev.get('model'))} · "
                f"<span class='{ok}'>{ev.get('duration_s')}s</span>{wsr}{cost} · "
                f"prompt {ev.get('prompt_chars','?')}c</span>")
        if ev.get("result"):
            head += f"\n      <span class='dim'>→ {esc(ev['result'])}</span>"
        if ev.get("prompt_preview"):
            head += f"\n      <span class='dim'>prompt: {esc(ev['prompt_preview'])}</span>"
        if ev.get("error"):
            head += f"\n      <span class='er'>{esc(ev['error'])}</span>"
        return head
    if k == "brand_done":
        return (ts + f"<span class='ok'>✓ DONE</span> {esc(ev.get('brand'))} "
                f"<span class='dim'>did={esc(ev.get('did'))} · {ev.get('status')} · "
                f"total {ev.get('duration_s')}s</span>")
    return ts + esc(ev)


if not events:
    st.info("No scan activity yet. Logs appear here live once the worker (or a "
            "manual scan / refresh) starts scanning brands.")
else:
    lines = "\n".join(render(e) for e in reversed(events))   # newest first
    st.markdown(f"<div class='term'>{lines}</div>", unsafe_allow_html=True)
    st.caption(f"{len(events)} recent events · newest first")

if auto and s["alive"] and s["state"] == "scanning":
    time.sleep(2.5)
    st.rerun()
