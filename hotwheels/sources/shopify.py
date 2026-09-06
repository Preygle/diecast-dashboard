"""Generic Shopify storefront adapter.

Most Indian specialty diecast shops run on Shopify, which exposes two public
JSON endpoints on every store. That means one adapter serves all of them and
adding a shop is a line of config, not a new scraper:

  /products.json?limit=250&page=N            full catalogue, paged
  /search/suggest.json?q=...                 keyword search, ~10 hits

Small specialist shops are read in `catalog` mode - fetching the whole
catalogue is both complete and cheaper than running 24 searches. Large general
retailers (a bookshop that also sells toys) use `search` mode instead, so we
do not page through 40,000 books to find ten cars.

Both payloads carry `vendor` (the brand) and `available` (stock), which is
better data than any marketplace HTML gives us.
"""

from __future__ import annotations

from typing import Any

from ..normalize import clean_text, is_target_diecast, parse_price
from .base import HttpSource, Item


def image_url(value: Any) -> str | None:
    """Coerce Shopify's several image shapes to a URL.

    /products.json gives {"src": ...}; /search/suggest.json gives either a bare
    string or an object with "url". Passing the object straight through put a
    dict into the database and aborted a whole scrape run.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        return image_url(value[0]) if value else None
    if isinstance(value, dict):
        for key in ("src", "url", "originalSrc", "path"):
            if value.get(key):
                return image_url(value[key])
    return None


class ShopifyStore(HttpSource):
    """One configured Shopify shop. Instances are built from config."""

    kind = "specialty"

    def __init__(self, cfg: Any, *, name: str, label: str, domain: str,
                 mode: str = "catalog", max_pages: int = 6) -> None:
        super().__init__(cfg)
        self.name = name
        self.label = label
        self.domain = domain.rstrip("/")
        self.mode = mode
        self.max_pages = max_pages

    @property
    def base(self) -> str:
        return f"https://{self.domain}"

    async def collect(self, queries: list[str]) -> list[Item]:
        # In catalog mode the queries are irrelevant - we read everything once
        # and filter locally, which is far kinder than 24 search requests.
        if self.mode == "catalog":
            return await self._catalog()
        return await super().collect(queries)

    async def _catalog(self) -> list[Item]:
        out: dict[str, Item] = {}
        async with self.json_client(headers={"Referer": self.base + "/"}) as client:
            for page in range(1, self.max_pages + 1):
                url = f"{self.base}/products.json?limit=250&page={page}"
                try:
                    resp = await client.get(url)
                except Exception as exc:
                    print(f"  [{self.name}] page {page} {type(exc).__name__}")
                    break
                if resp.status_code != 200:
                    print(f"  [{self.name}] page {page} HTTP {resp.status_code}")
                    break
                try:
                    products = resp.json().get("products", [])
                except Exception:
                    # A 200 that is not JSON means the edge served the HTML
                    # storefront instead of the API - a content-negotiation or
                    # bot-check answer. Silence here hid exactly that for a
                    # long time, so say it out loud.
                    ctype = resp.headers.get("content-type", "?")
                    print(f"  [{self.name}] page {page}: HTTP 200 but "
                          f"{ctype}, not JSON ({len(resp.content):,}b) - "
                          f"the shop served a page, not the API")
                    break
                if not products:
                    break
                kept_before = len(out)
                for p in products:
                    item = self._from_product(p)
                    if item:
                        out.setdefault(item.source_sku, item)
                # Raw vs kept separates "the shop served nothing" from "the
                # filters rejected everything" - they need opposite fixes.
                print(f"  [{self.name}] page {page}: {len(products)} raw, "
                      f"{len(out) - kept_before} kept")
                if len(products) < 250:
                    break
                await self.pause()
        return list(out.values())

    async def search(self, query: str) -> list[Item]:
        out: list[Item] = []
        async with self.json_client(headers={"Referer": self.base + "/"}) as client:
            resp = await client.get(
                f"{self.base}/search/suggest.json",
                params={
                    "q": query,
                    "resources[type]": "product",
                    "resources[limit]": 10,
                },
            )
            if resp.status_code != 200:
                return out
            try:
                products = resp.json()["resources"]["results"]["products"]
            except Exception:
                return out
            for p in products:
                item = self._from_suggestion(p)
                if item:
                    out.append(item)
        return out

    # ------------------------------------------------------------------
    def _from_product(self, p: dict[str, Any]) -> Item | None:
        """A product from /products.json (has full variant detail)."""
        title = clean_text(p.get("title") or "")
        vendor = p.get("vendor") or ""
        if not title or not is_target_diecast(title, vendor):
            return None

        variants = p.get("variants") or []
        if not variants:
            return None

        # Cheapest available variant; if none is available the product is out
        # of stock and the cheapest variant still gives its last known price.
        available = [v for v in variants if v.get("available")]
        pool = available or variants
        best = min(pool, key=lambda v: parse_price(v.get("price")) or 1e12)

        price = parse_price(best.get("price"))
        if not self.keep(price):
            return None

        images = p.get("images") or []
        return Item(
            source=self.name,
            source_sku=str(p.get("id") or p.get("handle")),
            region=self.region,
            raw_title=title,
            price=price,
            mrp=parse_price(best.get("compare_at_price")),
            url=f"{self.base}/products/{p.get('handle')}",
            image_url=image_url(images[0]) if images else None,
            in_stock=bool(available),
            brand_hint=vendor or None,
        )

    def _from_suggestion(self, p: dict[str, Any]) -> Item | None:
        """A product from /search/suggest.json (flatter, already priced)."""
        title = clean_text(p.get("title") or "")
        vendor = p.get("vendor") or ""
        if not title or not is_target_diecast(title, vendor):
            return None

        price = parse_price(p.get("price"))
        if not self.keep(price):
            return None

        url = p.get("url") or ""
        if url.startswith("/"):
            url = self.base + url

        return Item(
            source=self.name,
            source_sku=str(p.get("id") or p.get("handle") or title[:60]),
            region=self.region,
            raw_title=title,
            price=price,
            mrp=parse_price(p.get("compare_at_price_max")),
            url=url or None,
            image_url=image_url(p.get("featured_image")) or image_url(p.get("image")),
            in_stock=bool(p.get("available", True)),
            brand_hint=vendor or None,
        )
