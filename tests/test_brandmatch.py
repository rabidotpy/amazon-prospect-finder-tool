"""Brand↔domain post-verify (S2-2) + parked-detector FP guard (S2-3)."""
import pytest

import websearch as w

LEGIT = [
    ("RAW", "getrawnutrition.com"), ("Olipop", "drinkolipop.com"),
    ("Momentous", "livemomentous.com"), ("NAKED", "nakednutrition.com"),
    ("Animal", "animalpak.com"), ("Ascent", "ascentprotein.com"),
    ("NOW Foods", "nowfoods.com"), ("VEV", "vevnutra.com"),
    ("Maximum Human Performance", "mhpstrong.com"),
    ("Premier Research Labs", "prlabs.com"), ("5% Nutrition", "5percentnutrition.com"),
    ("SOHM", "store.sohm.com"), ("Nutricost", "nutricost.com"),
    ("Force Factor", "forcefactor.com"), ("Natures Craft", "shopnaturescraft.com"),
    ("BIRDMAN", "birdman.com"),
    # accented brands (diacritics folded) and multi-part TLDs (.co.nz/.co.kr)
    ("Horbäach", "horbaach.com"), ("Évolué Beverly Hills", "evolue.com"),
    ("BLIS", "blisprobiotics.co.nz"), ("Airborne", "airborne.co.nz"),
    ("VEGREEN", "vegreen.co.kr"),
]
WRONG = [
    ("FKMEE", "walkerskinprotect.com"), ("BIZYAC", "freshliving.store"),
    ("Tbexem", "daislashes.com"), ("Weslaplus", "olavitaskin.com"),
    ("Health Solution Prime", "vitaminprime.com"),
]


@pytest.mark.parametrize("brand,domain", LEGIT)
def test_legit_domains_accepted(brand, domain):
    assert w.brand_domain_match(brand, domain) is True


@pytest.mark.parametrize("brand,domain", WRONG)
def test_wrong_brand_domains_rejected(brand, domain):
    assert w.brand_domain_match(brand, domain) is False


def test_empty_inputs():
    assert w.brand_domain_match("", "x.com") is False
    assert w.brand_domain_match("Brand", "") is False


def test_is_parked_only_on_confirmed(monkeypatch):
    # redirect to /lander => parked
    monkeypatch.setattr(w, "http_get", lambda *a, **k:
                        '<script>window.location.href="/lander"</script>')
    assert w.is_parked("foo.com") is True
    # real store html => not parked
    monkeypatch.setattr(w, "http_get", lambda *a, **k:
                        "<title>Real Store</title><body>buy now, add to cart</body>")
    assert w.is_parked("foo.com") is False
    # unreachable (raises) => NOT parked (don't drop a blocked real site)
    def _boom(*a, **k):
        raise OSError("blocked")
    monkeypatch.setattr(w, "http_get", _boom)
    assert w.is_parked("foo.com") is False


@pytest.mark.parametrize("low,title,expected", [
    ("the domain nutritiononthego.com is for sale", "Domain For Sale", True),
    ("buy this domain now", "", True),
    ("hugedomains premium listing", "", True),
    # real store wording must NOT trip it (the shopnaturescraft regression)
    ("shop premium vitamins, buy now, items for sale today", "Shop Premium Vitamins", False),
    ("add to cart buy now best supplements", "Nutricost", False),
])
def test_parked_markers(low, title, expected):
    assert w._looks_parked(low, title, "example.com") is expected
