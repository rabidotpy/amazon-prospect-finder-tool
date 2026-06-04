"""Calculation/data fixes: price coercion (G4), comma-safe seller rollup (P1-3),
orphan pruning (P1-5), and the revenue gate (G2)."""
import pipeline
from conftest import h10_row, write_h10_csv, insert_brand


def test_to_float_strips_currency_and_commas():
    assert pipeline.to_float("$1,234.56") == 1234.56
    assert pipeline.to_float("2,500.00") == 2500.0
    assert pipeline.to_float("19.99") == 19.99
    for junk in ("", "N/A", "-", None, "abc"):
        assert pipeline.to_float(junk) is None


def test_price_stored_as_number(conn, tmp_path):
    p = str(tmp_path / "p.csv")
    rows = [h10_row("A1", "BrandX")]
    rows[0]["Price"] = "$1,234.56"
    write_h10_csv(rows, p)
    pipeline.ingest_file(conn, p)
    val = conn.execute("SELECT price, typeof(price) t FROM products WHERE asin='A1'").fetchone()
    assert val["t"] == "real" and abs(val["price"] - 1234.56) < 0.001


def test_comma_in_brand_does_not_inflate_seller_count(conn, tmp_path):
    p = str(tmp_path / "c.csv")
    write_h10_csv([h10_row("A1", "Pikl, LLC. PiKL Nutrition, LLC", seller="OneSeller")], p)
    pipeline.ingest_file(conn, p)
    pipeline.sync_brand_skeleton(conn)
    pipeline.rebuild_sellers(conn)
    import db
    r = conn.execute("SELECT brand_count FROM sellers WHERE seller_key=?",
                     (db.norm_key("OneSeller"),)).fetchone()
    assert r["brand_count"] == 1            # one real brand, not 3 comma-fragments


def test_rename_prunes_stale_brand_and_seller(conn, tmp_path):
    p1 = str(tmp_path / "old.csv")
    write_h10_csv([h10_row("A1", "Oldname", seller="OldSeller")], p1)
    pipeline.ingest_file(conn, p1)
    pipeline.sync_brand_skeleton(conn)
    pipeline.rebuild_sellers(conn)
    # re-import: same ASIN, corrected brand + seller
    p2 = str(tmp_path / "new.csv")
    write_h10_csv([h10_row("A1", "Newname", seller="NewSeller")], p2)
    pipeline.ingest_file(conn, p2)
    pipeline.sync_brand_skeleton(conn)
    pipeline.rebuild_sellers(conn)
    brands = {r["brand"] for r in conn.execute("SELECT brand FROM brands")}
    sellers = {r["seller"] for r in conn.execute("SELECT seller FROM sellers")}
    assert "Newname" in brands and "Oldname" not in brands     # stale pruned
    assert "NewSeller" in sellers and "OldSeller" not in sellers


def test_revenue_gate_excludes_low_value_website_work(conn, monkeypatch):
    import brand_scan
    monkeypatch.setattr(brand_scan, "ai_live_cached", lambda *a, **k: True)
    monkeypatch.setattr(brand_scan, "AI_REVENUE_FLOOR", 1000.0)
    # both have website pending (NULL), meta/google fresh (so only website could select them)
    fresh = "2099-01-01T00:00:00"
    insert_brand(conn, "rich", revenue=5000)
    insert_brand(conn, "poor", revenue=50)
    for k in ("rich", "poor"):
        conn.execute("UPDATE brands SET meta_scanned_at=?, google_scanned_at=?, "
                     "website_scanned_at=NULL WHERE brand_key=?", (fresh, fresh, k))
    conn.commit()
    keys = brand_scan.stale_brand_keys(conn)
    assert "rich" in keys           # worth researching
    assert "poor" not in keys       # below floor -> AI not spent
