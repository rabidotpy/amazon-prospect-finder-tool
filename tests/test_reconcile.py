"""Cross-signal reconcile: adopt the Google-verified advertiser domain as the
website when research found none (fixes the LEGION false-green / missing link)."""
import brand_scan
from conftest import insert_brand

GURL = "https://adstransparency.google.com/?region=anywhere&domain={d}"


def test_google_domain_extraction():
    assert brand_scan._google_domain(GURL.format(d="legion.co")) == "legion.co"
    assert brand_scan._google_domain("") == ""
    assert brand_scan._google_domain("https://x/?region=anywhere") == ""


def _set_google(conn, key, domain):
    conn.execute("UPDATE brands SET google_ads_url=? WHERE brand_key=?",
                 (GURL.format(d=domain), key))
    conn.commit()


def test_adopts_brand_matching_google_domain(conn):
    insert_brand(conn, "legion", has_website=0)
    conn.execute("UPDATE brands SET brand='LEGION', website_url='', confidence='none' "
                 "WHERE brand_key='legion'")
    _set_google(conn, "legion", "legion.co")
    adopted = brand_scan.reconcile_website(conn, "legion")
    assert adopted == "legion.co"
    r = conn.execute("SELECT website_url, has_website, confidence FROM brands "
                     "WHERE brand_key='legion'").fetchone()
    assert r["website_url"] == "legion.co" and r["has_website"] == 1
    assert r["confidence"] == "google_domain"


def test_does_not_adopt_mismatched_domain(conn):
    insert_brand(conn, "fkmee", has_website=0)
    conn.execute("UPDATE brands SET brand='FKMEE', website_url='' WHERE brand_key='fkmee'")
    _set_google(conn, "fkmee", "walkerskinprotect.com")
    assert brand_scan.reconcile_website(conn, "fkmee") is None
    r = conn.execute("SELECT website_url FROM brands WHERE brand_key='fkmee'").fetchone()
    assert (r["website_url"] or "") == ""


def test_does_not_overwrite_existing_website(conn):
    insert_brand(conn, "raw", has_website=1)
    conn.execute("UPDATE brands SET brand='RAW', website_url='getrawnutrition.com' "
                 "WHERE brand_key='raw'")
    _set_google(conn, "raw", "raw.co")
    assert brand_scan.reconcile_website(conn, "raw") is None
    r = conn.execute("SELECT website_url FROM brands WHERE brand_key='raw'").fetchone()
    assert r["website_url"] == "getrawnutrition.com"


def test_green_flips_when_website_adopted(conn):
    import green
    # no website + low known ads => would be 'green'; after adopting a website => not green
    insert_brand(conn, "legion", has_website=0, meta=0, google=5)
    conn.execute("UPDATE brands SET brand='LEGION', website_url='' WHERE brand_key='legion'")
    _set_google(conn, "legion", "legion.co")
    green.mark_one(conn, "legion")
    assert conn.execute("SELECT is_green FROM brands WHERE brand_key='legion'").fetchone()["is_green"] == 1
    brand_scan.reconcile_website(conn, "legion")
    green.mark_one(conn, "legion")
    assert conn.execute("SELECT is_green FROM brands WHERE brand_key='legion'").fetchone()["is_green"] == 0
