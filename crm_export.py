"""Turn a selected brand row into a GoHighLevel-ready lead.

A "lead" in GHL is a Contact (the company) optionally tied to an Opportunity
(the deal). GHL's CSV/JSON contact importer maps standard fields by header name
and treats every other column as a custom field, so we emit a single flat record
whose headers are exactly what the importer expects.

What we push and why:
  - companyName / website / source   -> identify and attribute the lead
  - tags                             -> segment in GHL (signal + has-ads flags)
  - opportunity name / value / stage -> seed the deal with revenue as value
  - custom fields                    -> every qualifying signal the rep needs to
                                        open a conversation (ad presence + counts
                                        + direct ad-library links, socials,
                                        revenue, reviews, listing age, an example
                                        product), so nothing is lost going to CRM.

We never invent contact emails/phones — those are blank for the rep to fill;
a blank field is honest, a guessed one is an error.
"""

import csv
import io
import json


def _yn(v):
    if v is None:
        return ""
    return "yes" if v else "no"


def _get(b, key, default=None):
    """Read a key from a sqlite3.Row or a plain dict without raising."""
    try:
        v = b[key]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def _website_list(b):
    raw = _get(b, "website_urls", "")
    if isinstance(raw, list):
        return raw
    try:
        out = json.loads(raw) if raw else []
        return out if isinstance(out, list) else []
    except Exception:
        return []


def brand_to_lead(b):
    """b: a sqlite3.Row / dict from the brands table. Returns a flat dict."""
    rev = b["parent_level_revenue"] or 0

    tags = ["amazon-prospect"]
    if b["prospect_signal"]:
        tags.append(b["prospect_signal"])
    if b["has_website"]:
        tags.append("has-website")
    else:
        tags.append("no-website")
    if b["is_meta_ads"] == 1:
        tags.append("runs-meta-ads")
    if b["is_google_ads"] == 1:
        tags.append("runs-google-ads")
    if b["is_meta_ads"] == 0 and b["is_google_ads"] == 0:
        tags.append("no-ads-detected")

    example_url = _get(b, "example_url", "")
    website_list = _website_list(b)

    lead = {
        # --- GHL standard contact fields ---
        "companyName": b["brand"],
        "website": b["website_url"] or (website_list[0] if website_list else ""),
        "website_candidates": " ; ".join(website_list),
        "website_remarks": _get(b, "website_remarks", "") or "",
        "source": "Helium10 Amazon prospecting",
        "tags": ", ".join(tags),
        "email": "",
        "phone": "",
        # --- GHL opportunity seed ---
        "opportunity_name": f'{b["brand"]} - DTC second revenue engine',
        "opportunity_stage": "New Lead",
        "opportunity_value": round(rev, 2),
        # --- custom fields: qualifying signals ---
        "prospect_signal": b["prospect_signal"] or "",
        "amazon_parent_revenue": round(rev, 2),
        "amazon_parent_sales_monthly": _get(b, "parent_level_sales", 0) or 0,
        "total_reviews": b["total_reviews"] or 0,
        "asin_count": b["asin_count"] or 0,
        "seller_count": b["seller_count"] or 0,
        "min_listing_age_months": b["min_listing_age"] or 0,
        "has_website": _yn(b["has_website"]),
        "website_confidence": b["confidence"] or "",
        "runs_meta_ads": _yn(b["is_meta_ads"]) if b["is_meta_ads"] is not None else "unknown",
        "meta_ads_count": b["meta_ads_count"] if b["meta_ads_count"] is not None else "",
        "meta_ad_library_url": b["meta_ads_url"] or "",
        "runs_google_ads": _yn(b["is_google_ads"]) if b["is_google_ads"] is not None else "unknown",
        "google_ads_count": b["google_ads_count"] if b["google_ads_count"] is not None else "",
        "google_ads_url": b["google_ads_url"] or "",
        "facebook": b["facebook"] or "",
        "instagram": b["instagram"] or "",
        "tiktok": b["tiktok"] or "",
        "youtube": b["youtube"] or "",
        "example_product_url": example_url or "",
    }
    return lead


def leads_to_csv(leads):
    """leads: list[dict]. Returns CSV text with a stable header order."""
    if not leads:
        return ""
    fields = list(leads[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for ld in leads:
        w.writerow(ld)
    return buf.getvalue()


def leads_to_json(leads):
    return json.dumps(leads, indent=2)
