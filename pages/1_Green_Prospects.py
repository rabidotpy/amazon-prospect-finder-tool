"""Green Prospects — the pitch list, served straight from the brands table.

A GREEN prospect is a brand with proven Amazon revenue but little/no
direct-to-consumer presence: no website and few/no ads. This page reads the
brands table live (no CSV round-trip) and lets a rep dial the definition in.

Links are outreach-ready: the Meta column is a direct **page** link
(view_all_page_id=…), the Google column is a direct **domain** transparency
link (?domain=…). Brand-name keyword-search links are never shown.
"""

import sqlite3

import pandas as pd
import streamlit as st

import config
import controls
import db
import green

st.set_page_config(page_title="Green Prospects", page_icon="🟢", layout="wide")
st.markdown(
    """
    <style>
      #MainMenu, footer {visibility: hidden;}
      .block-container {padding-top: 2.2rem; max-width: 1500px;}
      [data-testid="stMetricValue"] {color:#16a34a; font-weight:700;}
      [data-testid="stMetricLabel"] {opacity:.7;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def _conn():
    c = sqlite3.connect(db.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


@st.cache_data(ttl=30)
def load_brands():
    return pd.read_sql_query("SELECT * FROM brands", _conn())


def _https(u):
    """Make a bare domain clickable; pass through full URLs and blanks."""
    u = (u or "").strip()
    if not u:
        return ""
    return u if u.startswith("http") else "https://" + u


MAX = config.GREEN_MAX_ADS

controls.render_sidebar(_conn, load_brands.clear)

st.title("🟢 Green Prospects")
st.caption(f"Proven Amazon revenue, little/no DTC presence. Defaults match the "
           f"green rule: no website, under {MAX} ads on each network. "
           f"Meta = direct page link · Google = domain transparency link.")

top = st.columns([1, 6])
if top[0].button("🔄 Refresh"):
    load_brands.clear()
    st.rerun()

df = load_brands()
if df.empty:
    st.info("No brands yet. Import products and let the scanner run.")
    st.stop()

# ------------------------------------------------- classify (single source of truth)
def _count(v):
    return None if pd.isna(v) else int(v)


df = df.copy()
df["state"] = [
    green.green_state(0 if pd.isna(hw) else int(hw), _count(m), _count(g))
    for hw, m, g in zip(df["has_website"], df["meta_ads_count"], df["google_ads_count"])
]
n_green = int((df["state"] == "green").sum())
n_cand = int((df["state"] == "candidate").sum())

# --------------------------------------------------------------- filters
with st.container(border=True):
    r1 = st.columns([2.4, 1, 1])
    show = r1[0].segmented_control(
        "Show", ["Verified greens", "Candidates", "All prospects"],
        default="Verified greens",
        help="Verified = no website AND both ad counts known & under the cap. "
             "Candidates = no website but ads not yet scanned (unverified — never "
             "assumed zero).")
    rev_min = r1[1].number_input("Min revenue ($)", min_value=0, value=0, step=1000)
    min_reviews = r1[2].number_input("Min reviews", min_value=0, value=0, step=10)

    r2 = st.columns([1.4, 1, 1, 1, 1])
    website = r2[0].segmented_control("Has website?", ["Any", "No", "Yes"],
                                      default="Any")
    meta_min = r2[1].number_input("Meta ads ≥", min_value=0, value=0, step=1)
    meta_max = r2[2].number_input("Meta ads ≤", min_value=0, value=MAX, step=1)
    goog_min = r2[3].number_input("Google ads ≥", min_value=0, value=0, step=1)
    goog_max = r2[4].number_input("Google ads ≤", min_value=0, value=MAX, step=1)
    treat_unknown = st.toggle(
        "Count unknown ad counts as matching the range", value=True,
        help="ON: a brand whose ads aren't scanned yet still passes the ad-count "
             "filters (unknown ≠ zero, just not excluded). OFF: only brands with "
             "KNOWN counts in range pass.")

# --------------------------------------------------------------- apply
if show == "Candidates":
    view = df[df["state"] == "candidate"].copy()
elif show == "All prospects":
    view = df[df["state"].isin(["green", "candidate"])].copy()
else:
    view = df[df["state"] == "green"].copy()

if website == "No":
    view = view[view["has_website"].fillna(0) == 0]
elif website == "Yes":
    view = view[view["has_website"].fillna(0) == 1]

# Ad-count range filters. Unknown (NaN) counts are kept or dropped per the toggle —
# never silently treated as 0.
def _in_range(series, lo, hi):
    known = (series >= lo) & (series <= hi)
    return known | series.isna() if treat_unknown else known & series.notna()

view = view[_in_range(view["meta_ads_count"], meta_min, meta_max)]
view = view[_in_range(view["google_ads_count"], goog_min, goog_max)]

view = view[view["parent_level_revenue"].fillna(0) >= rev_min]
view = view[view["total_reviews"].fillna(0) >= min_reviews]
view = view.sort_values("parent_level_revenue", ascending=False)

# --------------------------------------------------------------- metrics
m1, m2, m3, m4 = st.columns(4)
with m1.container(border=True):
    st.metric("Matching prospects", f"{len(view):,}")
with m2.container(border=True):
    st.metric("Total monthly revenue",
              f"${view['parent_level_revenue'].fillna(0).sum():,.0f}")
with m3.container(border=True):
    st.metric("Verified greens", f"{n_green:,}",
              help="No website AND both ad counts known & under the cap.")
with m4.container(border=True):
    st.metric("Candidates (ads unverified)", f"{n_cand:,}",
              help="No website, but ads not yet scanned — not assumed zero.")

# --------------------------------------------------------------- table
out = pd.DataFrame()
out["Brand"] = view["brand"]
out["Revenue"] = view["parent_level_revenue"]
out["Reviews"] = view["total_reviews"]
out["ASINs"] = view.get("asin_count")
out["Signal"] = view.get("prospect_signal")
out["Website"] = view["website_url"].map(_https)
out["Meta ads"] = view["meta_ads_count"]
out["Meta page"] = view["meta_ads_url"].fillna("")
out["Google ads"] = view["google_ads_count"]
out["Google domain"] = view["google_ads_url"].fillna("")
out["Verified"] = view["meta_scanned_at"].notna()
out = out.reset_index(drop=True)

st.dataframe(
    out, use_container_width=True, hide_index=True, height=580,
    column_config={
        "Revenue": st.column_config.NumberColumn("Revenue", format="$%d"),
        "Reviews": st.column_config.NumberColumn("Reviews", format="%d"),
        "ASINs": st.column_config.NumberColumn("ASINs", format="%d"),
        "Meta ads": st.column_config.NumberColumn("Meta ads", format="%d"),
        "Google ads": st.column_config.NumberColumn("Google ads", format="%d"),
        "Website": st.column_config.LinkColumn(
            "Website", display_text=r"https?://(?:www\.)?([^/]+)"),
        "Meta page": st.column_config.LinkColumn("Meta page", display_text="ad library ↗"),
        "Google domain": st.column_config.LinkColumn("Google domain",
                                                     display_text="transparency ↗"),
        "Verified": st.column_config.CheckboxColumn("Scanned"),
    },
)

st.download_button("⬇️  Download CSV", out.to_csv(index=False),
                   file_name="green_prospects.csv", mime="text/csv")
