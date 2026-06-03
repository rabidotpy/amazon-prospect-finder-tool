"""Search + filter engine for the dashboard.

Two entry points, both returning a (products, brands, sellers) tuple of filtered
DataFrames so the UI can drop each into its own tab:

* run_search(...)   — ONE keyword box. Matches a substring across product brand,
  title, ASIN, category, subcategory and seller; the matched products then pull
  in their brands and sellers. Brand-name and seller-name direct hits are unioned
  in too, so typing a brand that has no product row still surfaces it.

* run_advanced(...) — explicit fields (ASIN, brand, seller, category,
  subcategory, title, price range, seller country). Filters products on the
  given criteria, then derives the matching brands and sellers.

Matching is deterministic substring (case-insensitive); brand ordering reuses
search.relevance so a closer keyword match floats up, with revenue as the
tiebreak.
"""

import pandas as pd

import search

PRODUCT_SEARCH_COLS = ("brand", "title", "asin", "category", "subcategory", "seller")


def _contains(series, needle):
    return series.fillna("").astype(str).str.lower().str.contains(
        str(needle).lower().strip(), regex=False)


# ---------------------------------------------------------------- single search

def search_products(products, q):
    if not q or not q.strip():
        return products
    mask = pd.Series(False, index=products.index)
    for c in PRODUCT_SEARCH_COLS:
        if c in products.columns:
            mask = mask | _contains(products[c], q)
    return products[mask]


def _derive(matched_products, brands, sellers, q=None):
    """Brands/sellers implied by the matched products, unioned with direct
    brand-name / seller-name keyword hits."""
    bkeys = set(matched_products.get("brand_key", pd.Series(dtype=str)).dropna())
    skeys = set(matched_products.get("seller_key", pd.Series(dtype=str)).dropna())
    bsel = brands[brands["brand_key"].isin(bkeys)]
    ssel = sellers[sellers["seller_key"].isin(skeys)]
    if q and q.strip():
        bsel = pd.concat(
            [bsel, brands[_contains(brands["brand"], q)]]
        ).drop_duplicates("brand_key")
        ssel = pd.concat(
            [ssel, sellers[_contains(sellers["seller"], q)]]
        ).drop_duplicates("seller_key")
    return bsel, ssel


def _rank_brands(bdf, q=None):
    out = bdf.copy()
    if q and q.strip():
        out["relevance"] = [search.relevance(q, b, fuzzy=True) for b in out["brand"]]
    else:
        out["relevance"] = 0.0
    sort_cols = ["relevance", "parent_level_revenue"] if "parent_level_revenue" in out \
        else ["relevance"]
    return out.sort_values(sort_cols, ascending=[False] * len(sort_cols))


def _rank_sellers(sdf, q=None):
    out = sdf.copy()
    if q and q.strip():
        out["relevance"] = [search.relevance(q, s, fuzzy=True) for s in out["seller"]]
    else:
        out["relevance"] = 0.0
    sort_cols = ["relevance", "parent_level_revenue"] if "parent_level_revenue" in out \
        else ["relevance"]
    return out.sort_values(sort_cols, ascending=[False] * len(sort_cols))


def run_search(products, brands, sellers, q):
    """Single-keyword search -> (products, brands, sellers) filtered + ranked."""
    mp = search_products(products, q)
    bsel, ssel = _derive(mp, brands, sellers, q)
    if q and q.strip():
        mp = mp.copy()
        mp["relevance"] = [search.relevance(q, t) for t in mp.get("title", "")]
        mp = mp.sort_values(["relevance", "parent_level_revenue"],
                            ascending=[False, False])
    return mp, _rank_brands(bsel, q), _rank_sellers(ssel, q)


# ---------------------------------------------------------------- advanced

def run_advanced(products, brands, sellers, *, asin="", brand="", seller="",
                 categories=None, subcategories=None, title="",
                 price_min=None, price_max=None, countries=None):
    """Explicit multi-field filter -> (products, brands, sellers)."""
    out = products
    if asin:
        out = out[_contains(out["asin"], asin)]
    if brand:
        out = out[_contains(out["brand"], brand)]
    if seller:
        out = out[_contains(out["seller"], seller)]
    if title:
        out = out[_contains(out["title"], title)]
    if categories:
        out = out[out["category"].isin(categories)]
    if subcategories:
        out = out[out["subcategory"].isin(subcategories)]
    if countries:
        out = out[out["seller_country"].isin(countries)]
    if price_min is not None:
        out = out[out["price"].fillna(0) >= price_min]
    if price_max is not None:
        out = out[out["price"].fillna(0) <= price_max]

    bsel, ssel = _derive(out, brands, sellers, q=None)
    if "parent_level_revenue" in out.columns:
        out = out.sort_values("parent_level_revenue", ascending=False)
    return out, _rank_brands(bsel), _rank_sellers(ssel)


# ---------------------------------------------------------------- filter options

def filter_options(products):
    """Distinct values for the advanced-filter dropdowns."""
    def vals(col):
        if col not in products.columns:
            return []
        return sorted(v for v in products[col].dropna().unique().tolist() if str(v).strip())
    price = products["price"].dropna() if "price" in products.columns else pd.Series(dtype=float)
    return {
        "categories": vals("category"),
        "subcategories": vals("subcategory"),
        "countries": vals("seller_country"),
        "price_min": float(price.min()) if not price.empty else 0.0,
        "price_max": float(price.max()) if not price.empty else 0.0,
    }
