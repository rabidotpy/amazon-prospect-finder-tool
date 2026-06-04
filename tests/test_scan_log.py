"""Structured scan-event log used by the live Scan Logs page."""
import pytest


@pytest.fixture
def sl(monkeypatch, tmp_path):
    import scan_log
    monkeypatch.setattr(scan_log, "LOG_PATH", str(tmp_path / "scan_events.log"))
    return scan_log


def test_emit_and_tail_roundtrip(sl):
    sl.emit("brand_start", brand="Nutricost", signals=["website", "meta"])
    sl.emit("ai_call", purpose="website_research", model="sonnet", duration_s=12.3)
    sl.emit("brand_done", brand="Nutricost", did=["website"], duration_s=20.1)
    evs = sl.tail()
    assert [e["kind"] for e in evs] == ["brand_start", "ai_call", "brand_done"]
    assert evs[0]["brand"] == "Nutricost" and evs[0]["signals"] == ["website", "meta"]
    assert evs[1]["model"] == "sonnet" and evs[1]["duration_s"] == 12.3
    assert all("ts" in e for e in evs)


def test_tail_empty_when_no_file(sl):
    assert sl.tail() == []


def test_clear(sl):
    sl.emit("note", msg="hi")
    assert sl.tail()
    sl.clear()
    assert sl.tail() == []


def test_size_cap_truncates(sl, monkeypatch):
    monkeypatch.setattr(sl, "_MAX_BYTES", 2000)
    monkeypatch.setattr(sl, "_KEEP_LINES", 20)
    for i in range(500):
        sl.emit("phase", brand=f"b{i}", signal="meta", result="x" * 30)
    evs = sl.tail(1000)
    assert len(evs) <= 40          # bounded, not 500
    assert evs[-1]["brand"] == "b499"   # newest kept
