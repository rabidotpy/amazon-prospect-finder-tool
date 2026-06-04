"""Structured scan-event log — the feed behind the live Scan Logs page.

Every stage of a scan appends one JSON line to data/scan_events.log:
  • brand_start  — a brand enters the pipeline (which signals it will scan)
  • phase        — a signal (website/meta/google) finished, with its duration+result
  • ai_call      — a claude call: purpose, model, duration, prompt preview, result, cost
  • brand_done   — a brand finished, with its total time
  • note         — any other important message

Because each brand is scanned in its OWN subprocess, they all append to the same
shared file (append mode, short lines) and the dashboard reads it. The file is
size-capped so it can't grow without bound.
"""

import datetime as dt
import json
import os

import db

LOG_PATH = os.path.join(os.path.dirname(db.DB_PATH), "scan_events.log")
_MAX_BYTES = 4_000_000
_KEEP_LINES = 1500


def emit(kind, **fields):
    """Append one structured event. Never raises into the caller.
    If SCAN_TRACE is set in the environment (a manual single-brand refresh), the
    event is tagged so the UI can show just that refresh's logs."""
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind}
    tr = os.environ.get("SCAN_TRACE")
    if tr:
        rec["trace"] = tr
    rec.update(fields)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(LOG_PATH) > _MAX_BYTES:
            _truncate()
    except Exception:
        pass


def _truncate():
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-_KEEP_LINES:])
        os.replace(tmp, LOG_PATH)
    except Exception:
        pass


def tail(n=250):
    """Most recent `n` events as dicts (oldest first)."""
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def clear():
    try:
        open(LOG_PATH, "w").close()
    except Exception:
        pass


# ----------------------------------------------------- terminal rendering (shared)
TERM_CSS = """
<style>
  .term {background:#0b1020; color:#d7e0f5; border-radius:10px; padding:14px 16px;
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size:12.5px; line-height:1.55; max-height:560px; overflow-y:auto;
         white-space:pre-wrap; border:1px solid #1e2a44;}
  .term .t{color:#5b6b8c;} .term .b{color:#7ee787;font-weight:600;}
  .term .ai{color:#79c0ff;} .term .ok{color:#7ee787;} .term .er{color:#ff7b72;}
  .term .ph{color:#e3b341;} .term .dim{color:#8b98b8;}
</style>
"""


def _esc(x):
    import html
    return html.escape(str(x))


def _line(ev):
    ts = f"<span class='t'>{_esc(ev.get('ts','')[11:])}</span> "
    k = ev.get("kind")
    if k == "brand_start":
        sigs = ", ".join(ev.get("signals", []))
        return (ts + f"<span class='b'>▶ START</span> {_esc(ev.get('brand'))} "
                f"<span class='dim'>· ${ev.get('revenue') or 0:,.0f} · "
                f"{_esc(ev.get('mode'))} · [{_esc(sigs)}]</span>")
    if k == "phase":
        if ev.get("error"):
            body = f"<span class='er'>error: {_esc(ev['error'])}</span>"
        elif ev.get("state"):
            body = f"<span class='dim'>{_esc(ev['state'])}</span>"
        else:
            d = (f" <span class='dim'>({ev.get('duration_s')}s)</span>"
                 if ev.get("duration_s") is not None else "")
            body = f"{_esc(ev.get('result',''))}{d}"
        return ts + f"  <span class='ph'>• {_esc(ev.get('signal',''))}</span> {body}"
    if k == "ai_call":
        ok = "ok" if ev.get("ok") else "er"
        cost = f" · ${ev['cost_usd']:.3f}" if ev.get("cost_usd") else ""
        wsr = f" · web×{ev['web_searches']}" if ev.get("web_searches") else ""
        s = (ts + f"  <span class='ai'>🤖 {_esc(ev.get('purpose'))}</span> "
             f"<span class='dim'>{_esc(ev.get('model'))} · "
             f"<span class='{ok}'>{ev.get('duration_s')}s</span>{wsr}{cost} · "
             f"prompt {ev.get('prompt_chars','?')}c</span>")
        if ev.get("result"):
            s += f"\n      <span class='dim'>→ {_esc(ev['result'])}</span>"
        if ev.get("prompt_preview"):
            s += f"\n      <span class='dim'>prompt: {_esc(ev['prompt_preview'])}</span>"
        if ev.get("error"):
            s += f"\n      <span class='er'>{_esc(ev['error'])}</span>"
        return s
    if k == "brand_done":
        return (ts + f"<span class='ok'>✓ DONE</span> {_esc(ev.get('brand'))} "
                f"<span class='dim'>did={_esc(ev.get('did'))} · {_esc(ev.get('status'))} "
                f"· total {ev.get('duration_s')}s</span>")
    return ts + _esc(ev)


def format_html(events, newest_first=True):
    """Render a list of events to the inner HTML of a .term terminal block."""
    seq = list(reversed(events)) if newest_first else list(events)
    return "\n".join(_line(e) for e in seq)
