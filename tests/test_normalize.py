"""Matching tests. Run: python -m pytest tests -q  (or: python tests/test_normalize.py)"""

from __future__ import annotations

import sys
from pathlib import Path

# The Windows console defaults to cp1252, which cannot encode the rupee sign
# these tests print. Without this the suite crashes on Windows but passes on CI.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import normalize as n  # noqa: E402


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


def main() -> int:
    fails = 0

    print("pack size")
    for title, want in [
        ("Hot Wheels 5-Car Pack of 1:64 Scale Vehicles", 5),
        ("HOT WHEELS 5 car gift pack", 5),
        ("Hot Wheels Basic Car, Pack of 10", 10),
        ("Hot Wheels Set of 20 Toy Cars", 20),
        ("Hot Wheels Nissan Skyline GT-R BNR34", 1),
        ("HOT WHEELS Monster Trucks Glow in the Dark (5 Monster Trucks)", 5),
        # "5 Alarm" is a model name, not a count - the noun must follow closely.
        ("Hot Wheels 5 Alarm Die Cast Car", 1),
    ]:
        fails += not check(title[:46], n.pack_size(title), want)

    print("price parsing")
    for raw, want in [("₹1,299.00", 1299.0), ("₹604", 604.0), ("2,029", 2029.0), ("", None)]:
        fails += not check(repr(raw), n.parse_price(raw), want)

    print("brand guard")
    for title, want in [
        ("Hot Wheels 5-Car Pack", True),
        ("HOT WHEELS Monster Trucks", True),
        ("Invite 1:32 Diecast Model Alloy B-M-W M4", False),
        ("SHINETOY 48pcs Pull Back Cars Set", False),
    ]:
        fails += not check(title[:46], n.looks_like_hotwheels(title), want)

    print("brand detection")
    for title, want in [
        ("Matchbox 1:64 Ford Bronco Die-Cast", "Matchbox"),
        ("MAJORETTE Premium Cars Assortment", "Majorette"),
        ("Tomica No.24 Nissan GT-R", "Tomica"),
        ("Mini GT 1:64 Lamborghini Huracan", "Mini GT"),
        ("Greenlight Collectibles 1:64 Muscle Series", "Greenlight"),
        ("Random Seller Alloy Toy Car", None),
        # Knockoffs ride the brand name in search; they are not the brand.
        ("BharatToys Hot Wheels Style Die Cast Toy Cars Set", None),
        ("Track compatible with Hot Wheels cars", None),
        ("Matchbox Type Mini Alloy Car", None),
        # Accessories name brands they fit; they are not those brands' cars.
        ("Enakshi RaceMedal 1:64 Doll Collections for Siku Matchbox Greenlight", None),
        ("DCO Kage Display Rack, 5-Tier, 1:64 Scale Diecast Car Stand", None),
        ("Acrylic Case Protector Pack for Hot Wheels", None),
        ("MAJORETTE - ROAD STICKING TAPE FOR DECOR AND DIORAMAS", None),
        # ...but a genuine Mattel display SET containing cars is a product.
        ("Hot Wheels Premium Collector Display Sets, 3 Cars", "Hot Wheels"),
        # Blinkit sells actual boxes of matches under the name "Matchbox".
        ("Big Matchbox by Homelites", None),
        ("Matchbox by Homelites", None),
        # Third-party keyword stuffing: brand buried late in the title.
        ("Arizuul 1:64 Dodge Challenger Diecast Model hot wheels cars under 100", None),
        # A short seller prefix before the brand is normal and must still pass.
        ("Mattel Hot Wheels 5-Car Pack 1:64", "Hot Wheels"),
        # Resellers who lead with the brand still must not pass.
        ("Arizuul hot wheels 1:64 1970 Dodge Challenger", None),
        ("BharatToys Hot Wheels 1:64 Car Set", None),
        # Deliberate misspellings that ride the brand.
        ("ZHASK All New Hot Wheelss 1/64 Scale Ford Raptor", None),
    ]:
        fails += not check(title[:46], n.detect_brand(title), want)

    print("1:64 scale gate")
    for title, want in [
        # Native 1:64 brands pass without stating a scale.
        ("Hot Wheels Nissan Skyline GT-R", True),
        ("Matchbox Ford Bronco Die-Cast", True),
        ("Majorette Porsche 911", True),
        # Multi-scale brands must state 1:64 or be dropped.
        ("Maisto Fresh Metal 1:64 Chevrolet", True),
        ("Maisto 1:24 Special Edition BMW M3", False),
        ("Bburago 1:18 Ferrari F40", False),
        # A native brand that explicitly says a wrong scale is still dropped.
        ("Hot Wheels 1:24 Scale RC Car", False),
        ("Tomica 1:87 Bus", False),
        # Non-brand junk never passes.
        ("SHINETOY 48pcs Pull Back Cars Set", False),
    ]:
        fails += not check(title[:46], n.is_target_diecast(title), want)

    print("brands never merge with each other")
    known0: dict[str, str] = {}
    ks = []
    for title in ["Hot Wheels Nissan Skyline GT-R BNR34",
                  "Matchbox Nissan Skyline GT-R BNR34"]:
        k = n.find_match(title, known0, 87)
        if k is None:
            k = n.canonical_key(title)
            known0[k] = n.key_signature(k)
        ks.append(k)
    fails += not check("same model, two brands stay separate", len(set(ks)), 2)

    print("cross-site grouping (the whole point)")
    same = [
        "Hot Wheels Basic Car 5-Pack, Multicolor, Set of 5 Toy Cars 1:64",
        "HOT WHEELS 5 Car Pack Assortment (Multicolor, Pack of 5)",
        "Hot Wheels 5 Car Pack",
    ]
    known: dict[str, str] = {}
    keys = []
    for title in same:
        k = n.find_match(title, known, 87)
        if k is None:
            k = n.canonical_key(title)
            known[k] = n.key_signature(k)
        keys.append(k)
    fails += not check("three titles collapse to one product", len(set(keys)), 1)

    # A single car must never be absorbed into a multipack.
    single = "Hot Wheels Basic Car 1:64 Scale Toy Car"
    k = n.find_match(single, known, 87)
    fails += not check("single car stays separate from 5-pack", k, None)

    print("distinct models must NOT merge")
    # Variants separated only by a number: the number is the identity.
    distinct = [
        "Hot Wheels Monster Trucks Promo 6, 1:64 Scale Die-Cast Monster Truck",
        "Hot Wheels Monster Trucks Promo 12, 1:64 Scale Die-Cast Monster Truck",
        "Hot Wheels Monster Trucks Promo 13, 1:64 Scale Die-Cast Monster Truck",
    ]
    known2: dict[str, str] = {}
    keys2 = []
    for title in distinct:
        k = n.find_match(title, known2, 87)
        if k is None:
            k = n.canonical_key(title)
            known2[k] = n.key_signature(k)
        keys2.append(k)
    fails += not check("Promo 6 / 12 / 13 stay separate", len(set(keys2)), 3)

    # Different Premium cars share a long boilerplate tail; token_set_ratio
    # merged these, token_sort_ratio must not.
    prem = [
        "Hot Wheels Premium Car Culture Collectible Toy Car 1:64 BMW E46 M3 Real Riders Tires",
        "Hot Wheels Premium Car Culture Collectible Toy Car 1:64 Nissan Skyline GT-R Real Riders Tires",
    ]
    known3: dict[str, str] = {}
    keys3 = []
    for title in prem:
        k = n.find_match(title, known3, 87)
        if k is None:
            k = n.canonical_key(title)
            known3[k] = n.key_signature(k)
        keys3.append(k)
    fails += not check("two Premium models stay separate", len(set(keys3)), 2)

    # Ages must not become identity.
    fails += not check(
        "age phrase stripped from signature",
        "3" in n.signature("Hot Wheels Lotus Elise Ages 3 and Up").split(),
        False,
    )

    print("realistic vs fantasy")
    for title, want in [
        # Licensed real cars, named by marque or by model alone.
        ("Hot Wheels Nissan Skyline GT-R BNR34", "Realistic"),
        ("Hot Wheels '69 Chevrolet Camaro", "Realistic"),
        ("Hot Wheels Porsche 911 Carrera RS", "Realistic"),
        ("Matchbox 1967 Jeepster Commando", "Realistic"),
        ("Hot Wheels 70 Road Runner Custom", "Realistic"),
        ("Hot Wheels Corvette Stingray", "Realistic"),
        # Hot Wheels original castings - no real-world counterpart.
        ("Hot Wheels Twin Mill", "Fantasy"),
        ("Hot Wheels Bone Shaker", "Fantasy"),
        ("Hot Wheels Rodger Dodger Toy Car", "Fantasy"),
        ("Hot Wheels Velocita - Checkmate", "Fantasy"),
        ("Hot Wheels Rollin' Solo - Blue", "Fantasy"),
        # Multipacks and track sets mix both, so the question does not apply.
        ("Hot Wheels 5-Car Pack of 1:64 Scale Vehicles", None),
        ("Hot Wheels Energy Track Set For Kids", None),
    ]:
        fails += not check(title[:44], n.detect_realism(title), want)

    print("series detection")
    for title, want in [
        ("Hot Wheels Monster Trucks Glow in the Dark", "Monster Trucks"),
        ("Hot Wheels Energy Track Set For Kids", "Track Set"),
        ("Hot Wheels Premium Collector Display Sets", "Premium"),
    ]:
        fails += not check(title[:46], n.detect_series(title), want)

    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
