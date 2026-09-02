"""FirstCry adapter.

FirstCry runs a custom ASP.NET storefront, so neither the Shopify nor the
WooCommerce adapter can reach it. It does, however, expose the same JSON
service its own listing pages page through:

  /svcs/ProductFilter.svc/GetSubcategoryWisePagingProducts?CatId=..&BrandId=..

That service is only addressable by category and brand id - its search sibling
(`SearchResult.svc`) ignores `SearchString` entirely and hands back the whole
479k-product catalogue, so searching through the API is not an option.

The way in is that `/search?q=<brand>` redirects to the brand's listing page,
whose markup carries the numeric ids in hidden inputs. So each query is
resolved once against the live site and then paged:

  brand hit   ids found, page the JSON service for the full catalogue
  no brand    fall back to the 20 server-rendered cards on the search page

Hot Wheels resolves to cat 5 / brand 113 and yields ~290 products this way,
against the 20 the HTML alone would give.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..normalize import clean_text, is_target_diecast, parse_price
from .base import HttpSource, Item

BASE = "https://www.firstcry.com"
IMG_CDN = "https://cdn.fcglcdn.com/brainbees/images/products/219x265/"

# Hidden inputs on a listing page that identify what is being listed.
HIDDEN = re.compile(
    r'<input[^>]*type="hidden"[^>]*id="(catid|brandid)"[^>]*value="([^"]*)"',
    re.I,
)

# One server-rendered product card. The aria-label on .li_inner_block is the
# only place the untruncated title appears; the visible one is ellipsised.
# Brand and search pages order that div's attributes differently and only the
# brand page fills in `listingpg-<id>`, so the tag is matched whole and the
# title and id are read out of it separately.
CARD = re.compile(r"<div[^>]*li_inner_block[^>]*>", re.I)
CARD_TITLE = re.compile(r'aria-label="([^"]*)"', re.I)
CARD_PID = re.compile(r'/(\d+)/product-detail|data-pid="(\d+)"')
CARD_PRICE = re.compile(r'aria-label="(?:Sale|Regular) price RS ([\d.,]+)', re.I)
CARD_MRP = re.compile(r"and Regular price RS ([\d.,]+)", re.I)
CARD_IMG = re.compile(r'src="(//cdn\.fcglcdn\.com/[^"]+)"')

PAGE_SIZE = 20


class FirstCry(HttpSource):
    """FirstCry, via its own listing service."""

    name = "firstcry"
    label = "FirstCry"
    kind = "marketplace"

    def __init__(self, cfg: Any) -> None:
        super().__init__(cfg)
        self.max_pages = int(cfg.source_opt(self.name, "max_pages", 15))
        # query -> (catid, brandid) | None, so a term is resolved once per run.
        self._resolved: dict[str, tuple[str, str] | None] = {}

    async def search(self, query: str) -> list[Item]:
        async with self.client(headers={"Referer": BASE + "/"}) as client:
            ids, html = await self._resolve(client, query)
            if ids is None:
                return self._parse_cards(html)
            return await self._page_service(client, *ids)

    # -- resolution ----------------------------------------------------
    async def _resolve(
        self, client: Any, query: str
    ) -> tuple[tuple[str, str] | None, str]:
        """Follow /search?q= and read the listing ids out of the landing page.

        Returns the ids when the query landed on a brand page, plus the HTML
        either way so a non-brand query can be parsed without a second fetch.
        """
        cached = self._resolved.get(query)
        if cached is not None:
            return cached, ""

        try:
            resp = await client.get(f"{BASE}/search", params={"q": query})
        except Exception as exc:
            print(f"  [{self.name}] search {query!r} failed: {type(exc).__name__}")
            return None, ""
        if resp.status_code != 200:
            return None, ""
        html = resp.text

        fields = {k.lower(): v for k, v in HIDDEN.findall(html)}
        catid, brandid = fields.get("catid", ""), fields.get("brandid", "")
        ids = (catid, brandid) if catid and brandid else None
        self._resolved[query] = ids
        return ids, html

    # -- the JSON service ----------------------------------------------
    async def _page_service(
        self, client: Any, catid: str, brandid: str
    ) -> list[Item]:
        out: list[Item] = []
        seen: set[str] = set()

        for page in range(1, self.max_pages + 1):
            products = await self._fetch_page(client, catid, brandid, page)
            if not products:
                break
            fresh = 0
            for p in products:
                pid = str(p.get("PId") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                fresh += 1
                item = self._parse(p)
                if item:
                    out.append(item)
            # A short page is the last one; no new ids means the service has
            # started repeating and there is nothing further to walk.
            if len(products) < PAGE_SIZE or fresh == 0:
                break
            await self.pause()

        return out

    async def _fetch_page(
        self, client: Any, catid: str, brandid: str, page: int
    ) -> list[dict[str, Any]]:
        params = {
            "PageNo": page, "PageSize": PAGE_SIZE, "SortExpression": "",
            "SubCatId": "", "BrandId": brandid, "CatId": catid,
            "Price": "", "Age": "", "Color": "", "OptionalFilter": "",
            # Out-of-stock listings are wanted: an item that exists here but
            # cannot be bought is exactly what the stock tracking is for.
            "OutOfStock": "true", "sorting": "", "searchrank": "",
        }
        try:
            resp = await client.get(
                f"{BASE}/svcs/ProductFilter.svc/GetSubcategoryWisePagingProducts",
                params=params,
                headers={"Accept": "application/json", "Referer": f"{BASE}/search"},
            )
            if resp.status_code != 200:
                return []
            body = resp.json()
        except Exception:
            return []

        # The payload double-encodes: the product list is a JSON string inside
        # a JSON object.
        raw = body.get("ProductResponse") if isinstance(body, dict) else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return []
        if not isinstance(raw, dict):
            return []
        products = raw.get("Products")
        return products if isinstance(products, list) else []

    def _parse(self, p: dict[str, Any]) -> Item | None:
        title = clean_text(p.get("PNm") or "")
        hint = clean_text(p.get("BNm") or "") or None
        if not title or not is_target_diecast(title, hint):
            return None

        price = parse_price(p.get("discprice"))
        mrp = parse_price(p.get("MRP"))
        if not self.keep(price):
            return None

        pid = str(p.get("PId") or "")
        images = [i for i in str(p.get("Images") or "").split(";") if i]
        stock = parse_price(p.get("CrntStock"))
        reviews = parse_price(p.get("review"))

        return Item(
            source=self.name,
            source_sku=pid,
            region=self.region,
            raw_title=title,
            price=price,
            mrp=mrp if (mrp and price and mrp > price) else None,
            url=self._product_url(pid),
            image_url=IMG_CDN + images[0] if images else None,
            in_stock=bool(stock and stock > 0),
            rating=parse_price(p.get("rating")),
            reviews=int(reviews) if reviews else None,
            brand_hint=hint,
        )

    # -- HTML fallback -------------------------------------------------
    def _parse_cards(self, html: str) -> list[Item]:
        """Read the 20 cards a search page renders server-side.

        Used for terms with no brand page behind them ("diecast car set"),
        where the JSON service has no ids to page.
        """
        out: list[Item] = []
        if not html:
            return out

        for block in html.split('class="list_block')[1:]:
            tag = CARD.search(block)
            pm_id = CARD_PID.search(block)
            if not tag or not pm_id:
                continue
            title_m = CARD_TITLE.search(tag.group(0))
            title = clean_text(title_m.group(1)) if title_m else ""
            pid = pm_id.group(1) or pm_id.group(2)
            if not title or not is_target_diecast(title):
                continue

            pm = CARD_PRICE.search(block)
            mm = CARD_MRP.search(block)
            price = parse_price(pm.group(1)) if pm else None
            mrp = parse_price(mm.group(1)) if mm else None
            if not self.keep(price):
                continue

            img = CARD_IMG.search(block)
            out.append(Item(
                source=self.name,
                source_sku=pid,
                region=self.region,
                raw_title=title,
                price=price,
                mrp=mrp if (mrp and price and mrp > price) else None,
                url=self._product_url(pid),
                image_url="https:" + img.group(1) if img else None,
                # No price rendered on the card is how FirstCry shows an item
                # it is not currently selling.
                in_stock=price is not None,
            ))
        return out

    @staticmethod
    def _product_url(pid: str) -> str:
        # FirstCry keys product-detail off the trailing id; the two slug
        # segments are decorative and any value resolves to the right page.
        return f"{BASE}/p/p/{pid}/product-detail"
