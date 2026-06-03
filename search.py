"""Relevance ranking for the keyword box.

Design goals (in priority order):
  1. Full/exact matches ALWAYS outrank partial matches.
  2. A brand-name hit outranks a seller hit, which outranks a product-title hit
     (the lead grain is the brand).
  3. Deterministic first: a query only matches text that genuinely contains it.
  4. Typos are caught by a GUARDED fuzzy fallback that can never displace a real
     match (lowest tier, high threshold, brand/seller names only).

No third-party dependencies — normalization via `unicodedata`, fuzzy via stdlib
`difflib`. At this scale (hundreds of rows) we score every row per keystroke;
no index needed.
"""

import difflib
import re
import unicodedata

# Tier scores. Gaps are wide so a higher tier can never be beaten by the
# revenue tiebreak of a lower tier.
EXACT = 100.0
STARTS = 85.0
ALL_WORDS = 75.0
ALL_PREFIX = 65.0
SUBSTRING = 55.0
PARTIAL_BASE = 30.0      # + up to 10 by fraction of query tokens present
FUZZY_BASE = 10.0        # + up to 5 by similarity; strictly the lowest tier
FUZZY_THRESHOLD = 0.86

# Field multipliers — brand name is the strongest signal.
W_BRAND = 1.0
W_SELLER = 0.7
W_TITLE = 0.6


def normalize(s):
    """Lowercase, strip accents, collapse punctuation/whitespace to spaces."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def relevance(query, value, fuzzy=False):
    """Return a tier score (0 = no match) for a single query/value pair.

    `fuzzy` enables the typo fallback — only pass True for short, clean fields
    (brand/seller names), never for noisy product titles.
    """
    q = normalize(query)
    v = normalize(value)
    if not q or not v:
        return 0.0
    # Compact forms ignore internal separators so a user query without the
    # brand's space/apostrophe still matches: "loreal"->"L'Oréal", "vivo"->"VivoNu".
    qc = q.replace(" ", "")
    vc = v.replace(" ", "")
    if v == q or vc == qc:
        return EXACT
    if v.startswith(q) or vc.startswith(qc):
        return STARTS

    qt = q.split()
    vt = v.split()
    vset = set(vt)

    if qt and all(t in vset for t in qt):
        return ALL_WORDS
    if qt and all(any(w.startswith(t) for w in vt) for t in qt):
        return ALL_PREFIX
    if q in v or qc in vc:
        return SUBSTRING

    hits = sum(1 for t in qt if t in v)
    if hits:
        return PARTIAL_BASE + 10.0 * (hits / len(qt))

    if fuzzy:
        best = 0.0
        for t in qt:
            for w in vt:
                best = max(best, difflib.SequenceMatcher(None, t, w).ratio())
        if best >= FUZZY_THRESHOLD:
            return FUZZY_BASE + 5.0 * best
    return 0.0


def brand_score(query, brand, sellers=(), titles=()):
    """Weighted best score for a brand across its name, sellers, and titles.

    Multipliers ensure an exact brand match (100) beats an exact title match
    (100 * 0.6 = 60), satisfying 'full keyword matches given priority'.
    """
    best = relevance(query, brand, fuzzy=True) * W_BRAND
    for s in sellers:
        best = max(best, relevance(query, s, fuzzy=True) * W_SELLER)
    for t in titles:
        best = max(best, relevance(query, t, fuzzy=False) * W_TITLE)
    return best
