"""Ingest invariants — ASIN uniqueness is load-bearing (revenue is per-ASIN)."""
import pipeline
from conftest import h10_row, write_h10_csv


def test_duplicate_asin_upserts_not_duplicates(conn, tmp_path):
    p = str(tmp_path / "dup.csv")
    write_h10_csv([h10_row("A1", "BrandX", rev=500),
                   h10_row("A1", "BrandX", rev=999)], p)  # same ASIN twice
    pipeline.ingest_file(conn, p)
    n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert n == 1                                   # one row, not two
    rev = conn.execute("SELECT asin_revenue FROM products WHERE asin='A1'").fetchone()[0]
    assert rev == 999.0                             # last write wins


def test_reimport_updates_existing(conn, tmp_path):
    p1 = str(tmp_path / "a.csv"); write_h10_csv([h10_row("A1", "BrandX", rev=500)], p1)
    pipeline.ingest_file(conn, p1)
    p2 = str(tmp_path / "b.csv"); write_h10_csv([h10_row("A1", "BrandX", rev=222)], p2)
    res = pipeline.ingest_file(conn, p2)
    assert res["updated"] == 1 and res["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_blank_asin_skipped(conn, tmp_path):
    p = str(tmp_path / "blank.csv")
    write_h10_csv([h10_row("", "NoAsin"), h10_row("B1", "RealBrand")], p)
    res = pipeline.ingest_file(conn, p)
    assert res["skipped"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_no_duplicate_asins_invariant(conn, tmp_path):
    p = str(tmp_path / "multi.csv")
    write_h10_csv([h10_row(f"A{i}", f"Brand{i % 3}") for i in range(30)]
                  + [h10_row("A0", "Brand0", rev=777)], p)  # A0 repeated
    pipeline.ingest_file(conn, p)
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT asin) FROM products").fetchone()[0]
    assert total == distinct == 30
