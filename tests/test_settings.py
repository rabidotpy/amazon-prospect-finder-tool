"""Daily scan cap — live setting + counter + run_job enforcement."""
import pytest


@pytest.fixture
def s(monkeypatch, tmp_path):
    import settings
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "COUNTER_PATH", str(tmp_path / "counter.json"))
    return settings


def test_default_cap_is_100(s):
    assert s.get_daily_cap() == 100


def test_set_cap_persists_and_is_live(s):
    s.set_daily_cap(250)
    assert s.get_daily_cap() == 250
    s.set_daily_cap(10)
    assert s.get_daily_cap() == 10          # re-read live
    assert s.set_daily_cap(-5) == 0         # clamped to >= 0


def test_counter_distinct_and_remaining(s):
    s.set_daily_cap(3)
    assert s.scanned_today() == 0 and s.remaining_today() == 3 and s.can_scan()
    s.record_scan("a"); s.record_scan("b"); s.record_scan("a")   # 'a' counted once
    assert s.scanned_today() == 2 and s.remaining_today() == 1
    s.record_scan("c")
    assert s.scanned_today() == 3 and not s.can_scan()           # cap reached


def test_lowering_cap_below_count_blocks_immediately(s):
    s.set_daily_cap(100)
    for k in "abcde":
        s.record_scan(k)
    assert s.can_scan()                     # 5 < 100
    s.set_daily_cap(5)                       # pull the cap down to exactly today's count
    assert not s.can_scan() and s.remaining_today() == 0
    s.set_daily_cap(4)                       # below the count
    assert s.remaining_today() == 0


def test_run_job_stops_at_cap(monkeypatch, tmp_path):
    """run_job must not launch brands once the live cap is hit."""
    import ad_jobs
    monkeypatch.setattr(ad_jobs, "JOBS_DIR", str(tmp_path / "jobs"))
    import os
    os.makedirs(ad_jobs.JOBS_DIR, exist_ok=True)
    monkeypatch.setattr(ad_jobs.green, "recompute_all", lambda c: 0)
    monkeypatch.setattr(ad_jobs.green, "export_csv", lambda c, *a, **k: 0)
    # cap already reached -> no brand should be launched
    monkeypatch.setattr(ad_jobs.settings, "can_scan", lambda: False)
    job = {"id": "cap", "kind": "scan", "brand_keys": ["__a__", "__b__", "__c__"],
           "brand_names": [], "force": False, "concurrency": 2, "total": 3,
           "done": 0, "results": [], "status": "queued"}
    ad_jobs._write_job(job)
    ad_jobs.run_job("cap")
    final = ad_jobs.read_job("cap")
    assert final["status"] == "done"
    assert final["done"] == 0               # cap blocked all launches
    assert final.get("capped") is True
