"""Shared pytest fixtures — every test runs against an isolated temp SQLite DB."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402


@pytest.fixture
def conn(monkeypatch, tmp_path):
    """A fresh, isolated DB per test (db.DB_PATH redirected to a temp file)."""
    import pipeline
    dbp = str(tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(pipeline, "IMPORTS_DIR", str(tmp_path / "imports"))
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def insert_brand(conn, key, *, has_website=0, meta=None, google=None,
                 revenue=1000.0, enriched="2026-01-01T00:00:00"):
    """Insert a brand row directly (bypassing the pipeline) for unit tests."""
    conn.execute(
        "INSERT INTO brands (brand_key, brand, has_website, meta_ads_count, "
        "google_ads_count, parent_level_revenue, enriched_at) VALUES (?,?,?,?,?,?,?)",
        (key, key.replace("_", " ").title(), has_website, meta, google,
         revenue, enriched))
    conn.commit()


# Helium10 export header (for ingest tests)
H10_HEADERS = [
    "URL", "Image URL", "ASIN", "Title", "Brand", "Fulfillment", "Category",
    "Subcategory", "Price", "Seller", "Seller Country/Region", "BSR",
    "Subcategory BSR", "ASIN Sales", "Parent Level Sales", "Review Count",
    "Number of Active Sellers", "Number of Images", "Variation Count",
    "ASIN Revenue", "Parent Level Revenue", "Reviews Rating", "Listing Age (Months)",
]


def h10_row(asin, brand, *, seller="Acme LLC", prev=1000, rev=500, title=None):
    d = {h: "" for h in H10_HEADERS}
    d.update({"ASIN": asin, "Title": title or f"{brand} {asin}", "Brand": brand,
              "Seller": seller, "Seller Country/Region": "US", "Category": "Health",
              "Subcategory": "Supplements", "Price": "19.99", "ASIN Revenue": rev,
              "Parent Level Revenue": prev, "Review Count": 10,
              "URL": f"https://amazon.com/dp/{asin}"})
    return d


def write_h10_csv(rows, path):
    import csv
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=H10_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
