"""Job engine: per-child timeout decision, interrupted-job detection, and a real
(cheap) pool run that exercises the heartbeat callback."""
import json
import os

import pytest


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    import ad_jobs
    monkeypatch.setattr(ad_jobs, "JOBS_DIR", str(tmp_path / "ad_jobs"))
    os.makedirs(ad_jobs.JOBS_DIR, exist_ok=True)
    return ad_jobs


def test_expired_deadline_pure():
    import ad_jobs
    assert ad_jobs._expired(100.0, 100.0 + ad_jobs.CHILD_TIMEOUT) is True
    assert ad_jobs._expired(100.0, 100.0 + ad_jobs.CHILD_TIMEOUT - 1) is False
    assert ad_jobs._expired(0, 5, timeout=10) is False
    assert ad_jobs._expired(0, 10, timeout=10) is True


def test_interrupted_jobs_detects_running(jobs):
    jobs._write_job({"id": "live", "status": "running", "total": 5, "done": 2,
                     "results": [{}, {}]})
    jobs._write_job({"id": "donejob", "status": "done", "total": 1, "done": 1,
                     "results": [{}]})
    ids = {j["id"] for j in jobs.interrupted_jobs()}
    assert "live" in ids and "donejob" not in ids


def test_scan_signal_passes_only_flag(monkeypatch):
    import ad_jobs
    captured = {}

    class _R:
        stdout = '{"brand_key":"x","status":"ok","did":["meta"],"meta_count":7}'

    monkeypatch.setattr(ad_jobs.subprocess, "run",
                        lambda cmd, **k: captured.setdefault("cmd", cmd) or _R())
    out = ad_jobs.scan_signal("x", "meta")
    assert "--only" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--only") + 1] == "meta"
    assert out["did"] == ["meta"] and out["meta_count"] == 7


def test_run_job_heartbeat_and_completion(jobs, monkeypatch):
    """A job over brand keys that don't exist: each brand_scan child returns
    'missing' fast (no AI/Playwright), so we exercise the pool + progress_cb cheaply."""
    job = {"id": "t1", "kind": "scan", "brand_keys": ["__nope_a__", "__nope_b__"],
           "brand_names": [], "force": False, "concurrency": 2, "total": 2,
           "done": 0, "results": [], "status": "queued"}
    jobs._write_job(job)
    # don't let the post-run green refresh touch a real DB
    monkeypatch.setattr(jobs.green, "recompute_all", lambda c: 0)
    monkeypatch.setattr(jobs.green, "export_csv", lambda c, *a, **k: 0)

    seen = []
    jobs.run_job("t1", progress_cb=lambda j, inflight: seen.append((j["done"], list(inflight))))

    final = jobs.read_job("t1")
    assert final["status"] == "done"
    assert final["done"] == 2
    statuses = {r.get("status") for r in final["results"]}
    assert statuses == {"missing"}          # both brands resolved (don't exist)
    assert seen                              # heartbeat callback fired
