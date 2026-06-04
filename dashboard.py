"""Amazon prospecting dashboard — a single clean search page over data/prospects.db.

Flow
----
1. Import products: drop a Helium10 export (CSV/XLSX) via "Add products", or click
   "Refresh from Downloads" to pull any new export sitting in ~/Downloads. Both
   dedupe on ASIN (one row per ASIN, since ASIN revenue is computed per ASIN).
2. Find leads: one keyword box (brand / product / ASIN / category / seller) OR an
   advanced filter. Matches fan out across products -> brands -> sellers and show
   in three tabs.
3. Scan ads: in the Brands tab, queue a background scan for brands whose
   website / Meta / Google signals are missing or stale (>30 days). Two brands are
   scanned at a time; results land back in the brands table.

Run:  streamlit run dashboard.py
"""

import sqlite3
import time

import pandas as pd
import streamlit as st

import ad_jobs
import brand_actions
import brand_scan
import controls
import db
import green
import pipeline
import query
import settings

st.set_page_config(page_title="Amazon Prospects", page_icon="🟢", layout="wide")

# ----------------------------------------------------------------- theme / CSS
st.markdown(
    """
    <style>
      #MainMenu, footer {visibility: hidden;}
      .block-container {padding-top: 2.2rem; max-width: 1400px;}
      h1, h2, h3 {letter-spacing: -0.01em;}
      [data-testid="stMetricValue"] {font-weight: 700;}
      .stButton>button {border-radius: 10px; font-weight: 600;}
      .stButton>button[kind="primary"] {background:#16a34a; border-color:#16a34a;}
      div[data-baseweb="tab-list"] {gap: 4px;}
      button[data-baseweb="tab"] {font-weight: 600;}
      .pill {display:inline-block; padding:2px 10px; border-radius:999px;
             font-size:0.78rem; font-weight:600;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------- data access
@st.cache_resource
def get_conn():
    conn = sqlite3.connect(db.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


@st.cache_data(ttl=30)
def load_products():
    return pd.read_sql_query("SELECT * FROM products", get_conn())


@st.cache_data(ttl=30)
def load_brands():
    return pd.read_sql_query("SELECT * FROM brands", get_conn())


@st.cache_data(ttl=30)
def load_sellers():
    return pd.read_sql_query("SELECT * FROM sellers", get_conn())


def _bust_caches():
    load_products.clear()
    load_brands.clear()
    load_sellers.clear()


def _ingest_conn():
    """A short-lived write connection (separate from the cached read one)."""
    c = db.connect()
    db.init_db(c)
    return c


# ----------------------------------------------------------------- import bar
def import_bar():
    left, right, _ = st.columns([1.1, 1.4, 3])
    with left:
        with st.popover("➕  Add products", use_container_width=True):
            st.caption("Upload a Helium10 Black Box export. Dedupes on ASIN.")
            up = st.file_uploader("CSV or XLSX", type=["csv", "xlsx", "xls"],
                                  key="uploader", label_visibility="collapsed")
            if up is not None:
                token = f"{up.name}:{up.size}"
                if st.session_state.get("_last_upload") != token:
                    with st.spinner(f"Importing {up.name}…"):
                        conn = _ingest_conn()
                        res = pipeline.ingest_upload(conn, up.name, up.getvalue())
                    st.session_state["_last_upload"] = token
                    _bust_caches()
                    st.success(f"{up.name}: +{res['inserted']} new, "
                               f"{res['updated']} updated, {res['skipped']} skipped.")
                    st.rerun()
    with right:
        if st.button("🔄  Refresh products from Downloads", use_container_width=True):
            with st.spinner("Scanning ~/Downloads for new exports…"):
                conn = _ingest_conn()
                nfiles, nrows = pipeline.ingest_new(conn)
                if nfiles:
                    pipeline.sync_brand_skeleton(conn)
                    pipeline.rebuild_sellers(conn)
            if nfiles:
                _bust_caches()
                st.success(f"Imported {nrows} products from {nfiles} file(s).")
                st.rerun()
            else:
                st.info("No new Helium10 exports found in ~/Downloads.")


# ----------------------------------------------------------------- search bar
def search_controls(products):
    mode = st.segmented_control(
        "Find leads", ["Quick search", "Advanced filter"],
        default="Quick search", label_visibility="collapsed")
    if mode == "Advanced filter":
        opts = query.filter_options(products)
        a, b, c = st.columns(3)
        crit = {
            "asin": a.text_input("ASIN"),
            "brand": b.text_input("Brand name"),
            "seller": c.text_input("Seller name"),
        }
        d, e, f = st.columns(3)
        crit["title"] = d.text_input("Product title contains")
        crit["categories"] = e.multiselect("Category", opts["categories"])
        crit["subcategories"] = f.multiselect("Subcategory", opts["subcategories"])
        g, h = st.columns([2, 2])
        pmin, pmax = opts["price_min"], max(opts["price_max"], opts["price_min"] + 1)
        crit["price_min"], crit["price_max"] = g.slider(
            "Price range ($)", float(pmin), float(pmax),
            (float(pmin), float(pmax)))
        crit["countries"] = h.multiselect("Seller country", opts["countries"])
        return ("advanced", crit)
    q = st.text_input(
        "Quick search", placeholder="Search brand, product, ASIN, category, or seller…",
        label_visibility="collapsed")
    return ("quick", q)


# ----------------------------------------------------------------- brands tab
SIGNALS = (("website_scanned_at", "website"), ("meta_scanned_at", "Meta"),
           ("google_scanned_at", "Google"))


def _stale_signals(row):
    return [name for col, name in SIGNALS if brand_scan._stale(row.get(col))]


def render_brands_tab(bdf, products):
    if bdf.empty:
        st.warning("No brands match these filters.")
        return
    bdf = bdf.copy()
    # website_url is stored as a bare domain (e.g. "nutricost.com"); LinkColumn
    # only opens a real URL, so prefix the scheme for display.
    if "website_url" in bdf.columns:
        bdf["website_url"] = bdf["website_url"].fillna("").map(
            lambda u: "" if not str(u).strip()
            else (u if str(u).startswith("http") else "https://" + str(u)))
    bdf["needs_scan"] = bdf.apply(lambda r: ", ".join(_stale_signals(r)) or "—", axis=1)
    need_keys = bdf.loc[bdf["needs_scan"] != "—", "brand_key"].tolist()

    # -- scan controls (never exceed the live daily cap) --
    remaining = settings.remaining_today()
    c1, c2, c3 = st.columns([1.4, 1.4, 2])
    with c1:
        n_scan = min(len(need_keys), remaining)
        if st.button(f"🛰️  Scan {n_scan} stale/new brands",
                     type="primary", use_container_width=True,
                     disabled=not need_keys or remaining <= 0):
            keys = need_keys[:remaining]
            job = ad_jobs.enqueue(keys,
                                  brand_names=bdf.loc[bdf["brand_key"].isin(keys),
                                                      "brand"].tolist())
            st.session_state["scan_jobs"] = [job["id"]] + st.session_state.get("scan_jobs", [])
            st.toast(f"Queued scan for {job['total']} brand(s).")
            st.rerun()
    with c2:
        all_keys = bdf["brand_key"].tolist()
        n_force = min(len(all_keys), remaining)
        if st.button(f"♻️  Re-scan {n_force} (force)",
                     use_container_width=True,
                     disabled=not all_keys or remaining <= 0):
            keys = all_keys[:remaining]
            job = ad_jobs.enqueue(keys, force=True,
                                  brand_names=bdf.loc[bdf["brand_key"].isin(keys),
                                                      "brand"].tolist())
            st.session_state["scan_jobs"] = [job["id"]] + st.session_state.get("scan_jobs", [])
            st.toast(f"Queued FORCE scan for {job['total']} brand(s).")
            st.rerun()
    with c3:
        if remaining <= 0:
            st.caption(f"⛔ Daily cap reached ({settings.scanned_today()}/"
                       f"{settings.get_daily_cap()}). Raise it in the sidebar ⚙️ Settings.")
        else:
            st.caption(f"Stale = missing or scanned > 30 days ago. Daily budget: "
                       f"**{remaining}** scans left today (cap {settings.get_daily_cap()}).")

    # -- table --
    cols = [c for c in [
        "brand", "relevance", "parent_level_revenue", "parent_level_sales",
        "total_reviews", "asin_count", "seller_count", "prospect_signal",
        "has_website", "website_url", "meta_ads_count", "google_ads_count",
        "is_green", "needs_scan", "updated_at",
        "meta_ads_url", "google_ads_url"] if c in bdf.columns]
    cfg = {
        "brand": st.column_config.TextColumn("Brand", width="medium"),
        "relevance": None,
        "parent_level_revenue": st.column_config.NumberColumn("Revenue", format="$%d"),
        "parent_level_sales": st.column_config.NumberColumn("Sales", format="%d"),
        "total_reviews": st.column_config.NumberColumn("Reviews", format="%d"),
        "asin_count": st.column_config.NumberColumn("ASINs", format="%d"),
        "seller_count": st.column_config.NumberColumn("Sellers", format="%d"),
        "prospect_signal": st.column_config.TextColumn("Signal"),
        "has_website": st.column_config.CheckboxColumn("Site?"),
        "meta_ads_count": st.column_config.NumberColumn("Meta ads"),
        "google_ads_count": st.column_config.NumberColumn("Google ads"),
        "is_green": st.column_config.CheckboxColumn("Green?"),
        "needs_scan": st.column_config.TextColumn("Needs scan"),
        "meta_ads_url": st.column_config.LinkColumn("Meta", display_text="open"),
        "google_ads_url": st.column_config.LinkColumn("Google", display_text="open"),
        "website_url": st.column_config.LinkColumn("Website", display_text="open"),
    }
    st.dataframe(bdf[cols], use_container_width=True, hide_index=True,
                 column_config=cfg, height=480)

    # -- per-brand workspace: re-scan signals + inspect products --
    st.markdown("##### 🔁 Refresh / inspect a brand")
    pick = st.selectbox("Brand", bdf["brand"].tolist(),
                        key="refresh_brand", label_visibility="collapsed")
    brow = bdf[bdf["brand"] == pick].iloc[0]
    with st.container(border=True):
        brand_actions.render_workspace(get_conn(), brow, bust_caches=_bust_caches,
                                       key_prefix="dash_")
    brand_actions.maybe_show_dialog(_bust_caches)

    render_job_panel()


def render_job_panel():
    job_ids = st.session_state.get("scan_jobs", [])
    jobs = [ad_jobs.read_job(j) for j in job_ids]
    jobs = [j for j in jobs if j] or ad_jobs.list_jobs(limit=4)
    if not jobs:
        return
    running = any(j.get("status") in ("queued", "running") for j in jobs)
    head = st.columns([2, 1, 1])
    head[0].markdown("##### Scan jobs")
    auto = head[1].toggle("Auto-refresh", value=running, key="job_auto",
                          help="Refresh this panel every few seconds while a scan runs.")
    if head[2].button("🔄 Refresh", key="job_refresh"):
        _bust_caches()
        st.rerun()
    if running:
        st.caption("A scan is in progress…" + (" auto-refreshing." if auto else
                   " toggle Auto-refresh or click Refresh to update."))
    for j in jobs:
        done, total = j.get("done", 0), j.get("total", 0)
        with st.expander(f"{j.get('status','?')} · {done}/{total} · {j.get('id','')}",
                         expanded=running):
            if total:
                st.progress(min(done / total, 1.0))
            if j.get("results"):
                st.dataframe(pd.DataFrame(j["results"]), use_container_width=True,
                             hide_index=True)
    if running and auto:
        time.sleep(3)
        _bust_caches()
        st.rerun()


# ----------------------------------------------------------------- generic tabs
def render_products_tab(pdf):
    cols = [c for c in ["asin", "title", "brand", "seller", "category", "subcategory",
                        "price", "asin_sales", "asin_revenue", "parent_level_revenue",
                        "review_count", "reviews_rating", "listing_age_months",
                        "seller_country", "fulfillment", "url"] if c in pdf.columns]
    st.caption(f"{len(pdf):,} products")
    cfg = {
        "price": st.column_config.NumberColumn("Price", format="$%.2f"),
        "asin_revenue": st.column_config.NumberColumn("ASIN rev", format="$%d"),
        "parent_level_revenue": st.column_config.NumberColumn("Parent rev", format="$%d"),
        "url": st.column_config.LinkColumn("URL", display_text="open"),
    }
    st.dataframe(pdf[cols].head(2000), use_container_width=True, hide_index=True,
                 column_config=cfg, height=520)


def render_sellers_tab(sdf):
    cols = [c for c in ["seller", "seller_country", "brand_count", "product_count",
                        "parent_level_revenue", "parent_level_sales", "avg_rating",
                        "active_sellers", "brands"] if c in sdf.columns]
    st.caption(f"{len(sdf):,} sellers")
    cfg = {
        "parent_level_revenue": st.column_config.NumberColumn(
            "Est. revenue", format="$%d",
            help="Sum of ASIN revenue across this seller's products."),
        "parent_level_sales": st.column_config.NumberColumn("Sales", format="%d"),
        "avg_rating": st.column_config.NumberColumn("Rating", format="%.2f"),
    }
    st.dataframe(sdf[cols].head(2000), use_container_width=True, hide_index=True,
                 column_config=cfg, height=520)


# ----------------------------------------------------------------- main
def main():
    st.title("🟢 Amazon brand prospecting")
    st.caption("Import Helium10 exports → search across products, brands & sellers "
               "→ scan brands for Meta/Google ads. "
               "See the **Green Prospects** page for pitch-ready leads.")

    controls.render_sidebar(get_conn, _bust_caches)
    import_bar()
    products = load_products()
    brands = load_brands()
    sellers = load_sellers()

    if products.empty:
        st.info("No products yet. Use **Add products** or **Refresh from Downloads**.")
        return

    st.divider()
    mode, value = search_controls(products)
    if mode == "advanced":
        pdf, bdf, sdf = query.run_advanced(products, brands, sellers, **value)
    else:
        pdf, bdf, sdf = query.run_search(products, brands, sellers, value)

    # headline metrics
    m1, m2, m3, m4 = st.columns(4)
    with m1.container(border=True):
        st.metric("Brands", f"{len(bdf):,}")
    with m2.container(border=True):
        st.metric("Products", f"{len(pdf):,}")
    with m3.container(border=True):
        st.metric("Sellers", f"{len(sdf):,}")
    with m4.container(border=True):
        rev = bdf["parent_level_revenue"].fillna(0).sum() if "parent_level_revenue" in bdf else 0
        st.metric("Matched revenue", f"${rev:,.0f}")

    tab_b, tab_p, tab_s = st.tabs(
        [f"Brands ({len(bdf)})", f"Products ({len(pdf)})", f"Sellers ({len(sdf)})"])
    with tab_b:
        render_brands_tab(bdf, products)
    with tab_p:
        render_products_tab(pdf)
    with tab_s:
        render_sellers_tab(sdf)


if __name__ == "__main__":
    main()
