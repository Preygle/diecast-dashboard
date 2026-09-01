"""FirstCry parsing.

Two shapes have to keep working, because the adapter reads both: the JSON the
listing service returns for a brand page, and the cards a search page renders
server-side. The card markup is the fragile half - brand and search pages order
the same div's attributes differently and only the brand page fills in
`listingpg-<id>` - so both variants are pinned here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import config  # noqa: E402
from hotwheels.sources.firstcry import FirstCry  # noqa: E402


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


# One product exactly as the listing service returns it, trimmed to the fields
# the adapter reads.
PRODUCT = {
    "PId": "23076120",
    "PNm": "Hot Wheels 70 Ford Escort (161/250) Die Cast Free Wheel Toy Car - Brown",
    "BNm": "Hot Wheels",
    "MRP": "199",
    "discprice": "179",
    "CrntStock": "3",
    "rating": 4.43,
    "review": 5,
    "Images": "23076120a.jpg;23076120b.jpg;",
}

# The brand-page card: class first, id present.
BRAND_CARD = '''
<div class="list_block lft " data-outstock="true">
<div tabindex="0" class="li_inner_block listingpg-24194173"
     aria-label="Hot Wheels Formula 1 Toy Cars 10-Pack, 1:64 Scale Die-Cast Race Cars">
<a href="//www.firstcry.com/hot-wheels/x/24194173/product-detail"></a>
<img src="//cdn.fcglcdn.com/brainbees/images/products/219x265/24194173a.jpg">
<div class="rupee fw lft" aria-label="Sale price RS 1602 and Regular price RS 1780"></div>
</div></div>
'''

# The search-page card: aria-label first, id blank, pid only in the href.
SEARCH_CARD = '''
<div class="list_block lft ">
<div tabindex="0" role="checkbox"
     aria-label="Hot Wheels Premium Collector Display Sets, 3 Cars 1:64 - White"
     class="li_inner_block listingpg-">
<a href="/hot-wheels/x/21497090/product-detail?sterm=&spos=1"></a>
<img src="//cdn.fcglcdn.com/brainbees/images/products/219x265/21497090a.jpg">
<div class="rupee fw lft" aria-label="Regular price RS 530"></div>
</div></div>
'''

# A card for something we do not track, to prove the diecast gate still bites.
OFF_TARGET_CARD = '''
<div class="list_block lft ">
<div class="li_inner_block listingpg-8931801"
     aria-label="Cetaphil Sun SPF 50 Sunscreen Light Gel 50 ml"></div>
<a href="/cetaphil/x/8931801/product-detail"></a>
<div class="rupee fw lft" aria-label="Regular price RS 1299"></div>
</div>
'''


def main() -> int:
    cfg = config.load("config.yaml")
    fc = FirstCry(cfg)
    ok = True

    print("service JSON")
    item = fc._parse(PRODUCT)
    ok &= check("parsed", item is not None, True)
    if item:
        ok &= check("price is discprice, not MRP", item.price, 179.0)
        ok &= check("mrp kept when above price", item.mrp, 199.0)
        ok &= check("sku", item.source_sku, "23076120")
        ok &= check("in stock from CrntStock", item.in_stock, True)
        ok &= check("rating", item.rating, 4.43)
        ok &= check("reviews", item.reviews, 5)
        ok &= check("url is id-addressed",
                    item.url, "https://www.firstcry.com/p/p/23076120/product-detail")
        ok &= check("image is the first of the list", item.image_url,
                    "https://cdn.fcglcdn.com/brainbees/images/products/"
                    "219x265/23076120a.jpg")

    print("zero stock")
    sold_out = fc._parse({**PRODUCT, "CrntStock": "0"})
    ok &= check("kept but flagged out of stock",
                (sold_out is not None, sold_out.in_stock if sold_out else None),
                (True, False))

    print("MRP equal to price")
    no_disc = fc._parse({**PRODUCT, "MRP": "179"})
    ok &= check("no phantom discount", no_disc.mrp if no_disc else "missing", None)

    print("price cap")
    # The shipped config has no ceiling, so pin one here rather than read it.
    fc.max_price = 500.0
    ok &= check("above the ceiling is dropped",
                fc._parse({**PRODUCT, "discprice": "501"}), None)
    ok &= check("at the ceiling is kept",
                fc._parse({**PRODUCT, "discprice": "500"}) is not None, True)
    fc.max_price = None
    ok &= check("no ceiling keeps a dear listing",
                fc._parse({**PRODUCT, "discprice": "99999"}) is not None, True)

    print("brand-page card")
    items = fc._parse_cards(BRAND_CARD)
    ok &= check("one card parsed", len(items), 1)
    if items:
        ok &= check("sale price wins", items[0].price, 1602.0)
        ok &= check("regular price becomes mrp", items[0].mrp, 1780.0)
        ok &= check("id from listingpg", items[0].source_sku, "24194173")

    print("search-page card")
    items = fc._parse_cards(SEARCH_CARD)
    ok &= check("one card parsed", len(items), 1)
    if items:
        ok &= check("id recovered from href", items[0].source_sku, "21497090")
        ok &= check("single price, no mrp", (items[0].price, items[0].mrp),
                    (530.0, None))

    print("non-diecast card")
    ok &= check("off-target listing dropped",
                len(fc._parse_cards(OFF_TARGET_CARD)), 0)

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
