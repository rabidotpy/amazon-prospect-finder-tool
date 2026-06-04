"""Worker heartbeat/status — the model the dashboard reads to keep the user informed."""
import datetime as dt
import json

import pytest


@pytest.fixture
def ws(monkeypatch, tmp_path):
    import worker_status as w
    monkeypatch.setattr(w, "STATUS_PATH", str(tmp_path / "worker_status.json"))
    return w


def _set_heartbeat(ws, iso):
    data = ws.read()
    data["heartbeat"] = iso
    with open(ws.STATUS_PATH, "w") as fh:
        json.dump(data, fh)


def test_no_status_means_not_alive(ws):
    assert ws.read() is None
    assert ws.is_alive() is False
    assert ws.summary()["alive"] is False
    assert ws.summary()["state"] == "unknown"


def test_fresh_heartbeat_is_alive(ws):
    ws.write("scanning", job_id="abc", current="Nutricost", done=3, total=50)
    assert ws.is_alive() is True
    s = ws.summary()
    assert s["alive"] and s["state"] == "scanning"
    assert s["current"] == "Nutricost" and s["done"] == 3 and s["total"] == 50


def test_stale_heartbeat_is_not_alive(ws):
    ws.write("scanning", current="X")
    old = (dt.datetime.now() - dt.timedelta(seconds=ws.STALE_SECONDS + 30)).isoformat()
    _set_heartbeat(ws, old)
    assert ws.is_alive() is False
    # summary downgrades a stale 'scanning' to 'stopped' so the UI shows the truth
    assert ws.summary()["state"] == "stopped"
    assert ws.summary()["age"] > ws.STALE_SECONDS


def test_stopped_state_is_not_alive_even_if_fresh(ws):
    ws.write("stopped")
    assert ws.is_alive() is False
    assert ws.summary()["state"] == "stopped"


def test_started_at_preserved_across_writes(ws):
    first = ws.write("starting")
    started = first["started_at"]
    ws.write("scanning", current="A")
    ws.write("idle")
    assert ws.read()["started_at"] == started


def test_idle_clears_current(ws):
    ws.write("scanning", current="Busy")
    ws.write("idle")
    assert ws.summary()["current"] is None
