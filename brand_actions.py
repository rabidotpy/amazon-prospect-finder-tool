"""Shared per-brand actions used by BOTH the dashboard Brands tab and the Green
Prospects page: a per-signal refresh (Website / Meta / Google) with a live-log
popup, and a product drill-down workspace. Keeping this in one place means the two
pages behave identically and there's a single source of truth.
"""
import time

import pandas as pd
import streamlit as st

import ad_jobs
import scan_log


# ------------------------------------------------------------- per-signal refresh
def refresh_buttons(brand, brand_key, key_prefix=""):
    """Three scoped refresh buttons. Launches a background single-signal scan and
    arms the live-log dialog (shown by maybe_show_dialog)."""
    cols = st.columns(3)

    def _start(sig):
        trace, raw = ad_jobs.scan_signal_async(brand_key, sig)
        st.session_state["refresh_active"] = {
            "brand": brand, "key": brand_key, "signal": sig, "trace": trace, "raw": raw}
        st.rerun()

    if cols[0].button("🌐 Website", key=f"{key_prefix}rf_web", use_container_width=True):
        _start("website")
    if cols[1].button("📘 Meta", key=f"{key_prefix}rf_meta", use_container_width=True):
        _start("meta")
    if cols[2].button("🔎 Google", key=f"{key_prefix}rf_goog", use_container_width=True):
        _start("google")


@st.dialog("🖥️  Live refresh logs", width="large")
def _refresh_dialog(bust_caches):
    info = st.session_state.get("refresh_active")
    if not info:
        return
    st.markdown(f"Refreshing **{info['signal']}** for **{info['brand']}**")
    evs = [e for e in scan_log.tail(500) if e.get("trace") == info["trace"]]
    done = any(e.get("kind") == "brand_done" for e in evs)

    st.markdown(scan_log.TERM_CSS, unsafe_allow_html=True)
    inner = scan_log.format_html(evs) if evs else "<span class='dim'>starting…</span>"
    st.markdown(f"<div class='term'>{inner}</div>", unsafe_allow_html=True)

    try:
        raw = open(info["raw"]).read()[-4000:]
    except Exception:
        raw = ""
    if raw.strip():
        with st.expander("Raw scraper output"):
            st.code(raw)

    cols = st.columns([1, 1, 3])
    cols[0].success("✓ Done") if done else cols[0].info("⏳ Scanning…")
    if cols[1].button("Close", key="rf_close"):
        bust_caches()
        st.session_state.pop("refresh_active", None)
        st.rerun()
    if not done:
        time.sleep(1.5)
        st.rerun()


def maybe_show_dialog(bust_caches=lambda: None):
    """Render the live-log dialog if a refresh is in flight."""
    if st.session_state.get("refresh_active"):
        _refresh_dialog(bust_caches)


# ------------------------------------------------------------- product drill-down
_PROD_COLS = ["asin", "title", "price", "asin_sales", "asin_revenue",
              "review_count", "reviews_rating", "bsr", "category", "subcategory",
              "listing_age_months", "fulfillment", "seller", "seller_country", "url"]
_PROD_CFG = {
    "price": st.column_config.NumberColumn("Price", format="$%.2f"),
    "asin_sales": st.column_config.NumberColumn("Sales/mo", format="%d"),
    "asin_revenue": st.column_config.NumberColumn("Revenue/mo", format="$%d"),
    "review_count": st.column_config.NumberColumn("Reviews", format="%d"),
    "reviews_rating": st.column_config.NumberColumn("Rating", format="%.1f"),
    "bsr": st.column_config.NumberColumn("BSR", format="%d"),
    "listing_age_months": st.column_config.NumberColumn("Age (mo)", format="%d"),
    "url": st.column_config.LinkColumn("Amazon", display_text="open ↗"),
}


def render_products(conn, brand_key, key_prefix=""):
    """Show the products a brand sells + a full-detail inspector for one product."""
    pf = pd.read_sql_query("SELECT * FROM products WHERE brand_key=?",
                           conn, params=[brand_key])
    if pf.empty:
        st.caption("No products on file for this brand.")
        return
    st.markdown(f"**🛒 Products this brand sells ({len(pf)})**")
    cols = [c for c in _PROD_COLS if c in pf.columns]
    cfg = {k: v for k, v in _PROD_CFG.items() if k in cols}
    st.dataframe(pf[cols].sort_values("asin_revenue", ascending=False),
                 use_container_width=True, hide_index=True, column_config=cfg,
                 height=min(360, 60 + 35 * len(pf)))

    # full detail of one product (every stored field)
    asin = st.selectbox("Inspect a product — all fields", pf["asin"].tolist(),
                        key=f"{key_prefix}prod_asin")
    rowi = pf[pf["asin"] == asin].iloc[0]
    detail = pd.DataFrame({"field": rowi.index, "value": rowi.astype(str).values})
    detail = detail[detail["value"].str.strip().isin(["", "None", "nan"]) == False]
    st.dataframe(detail, use_container_width=True, hide_index=True, height=420)


def render_workspace(conn, brand_row, bust_caches=lambda: None, key_prefix="ws_"):
    """The whole per-brand workspace: signal summary + re-scan + products."""
    brand = brand_row["brand"]
    brand_key = brand_row["brand_key"]

    def _num(v):
        return "—" if pd.isna(v) else f"{int(v)}"

    m = st.columns(4)
    m[0].metric("Revenue/mo", f"${(brand_row.get('parent_level_revenue') or 0):,.0f}")
    site = (brand_row.get("website_url") or "").strip()
    m[1].metric("Website", site or "—")
    m[2].metric("Meta ads", _num(brand_row.get("meta_ads_count")))
    m[3].metric("Google ads", _num(brand_row.get("google_ads_count")))

    st.markdown("**🔁 Re-scan a signal** (live logs pop up; not counted against the daily cap)")
    refresh_buttons(brand, brand_key, key_prefix=key_prefix)

    st.divider()
    render_products(conn, brand_key, key_prefix=key_prefix)
