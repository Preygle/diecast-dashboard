"""Generic WooCommerce Store API adapter.

WooCommerce ships a public, unauthenticated Store API that most shops leave
enabled:

  /wp-json/wc/store/v1/products?search=...&per_page=100

Prices arrive as integer strings in the currency's minor unit, with the scale
given per response (`currency_minor_unit`), so "49900" with minor unit 2 is
Rs 499.00. Reading that field rather than assuming paise keeps a shop that
reports whole rupees from being priced at 1/100th.
"""

from __future__ import annotations

from typing import Any

from ..normalize import clean_text, is_target_diecast, parse_price
from .base import HttpSource, Item
from .shopify import image_url


class WooStore(HttpSource):
    """One configured WooCommerce shop."""

    kind = "specialty"

    def __init__(self, cfg: Any, *, name: str, label: str, domain: str,
                 per_page: int = 50) -> None:
        super().__init__(cfg)
        self.name = name
        self.label = label
        self.domain = domain.rstrip("/")
        self.per_page = per_page

    @property
    def base(self) -> str:
        return f"https://{self.domain}"

    async def search(self, query: str) -> list[Item]:
        out: list[Item] = []
        async with self.client(headers={"Referer": self.base + "/"}) as client:
            resp = await client.get(
                f"{self.base}/wp-json/wc/store/v1/products",
                params={"search": query, "per_page": self.per_page},
            )
            if resp.status_code != 200:
                return out
            try:
                products = resp.json()
            except Exception:
                return out
            if not isinstance(products, list):
                return out
            for p in products:
                item = self._parse(p)
                if item:
                    out.append(item)
        return out

    # ------------------------------------------------------------------
    def _parse(self, p: dict[str, Any]) -> Item | None:
        title = clean_text(p.get("name") or "")
        # Woo has no vendor field; brands usually appear as attributes.
        hint = self._brand_attr(p)
        if not title or not is_target_diecast(title, hint):
            return None

        prices = p.get("prices") or {}
        minor = int(prices.get("currency_minor_unit", 2) or 0)
        scale = 10 ** minor

        price = self._money(prices.get("price"), scale)
        mrp = self._money(prices.get("regular_price"), scale)
        if not self.keep(price):
            return None

        images = p.get("images") or []
        return Item(
            source=self.name,
            source_sku=str(p.get("id")),
            region=self.region,
            raw_title=title,
            price=price,
            mrp=mrp if (mrp and price and mrp > price) else None,
            url=p.get("permalink"),
            image_url=image_url(images[0]) if images else None,
            in_stock=bool(p.get("is_in_stock", True)),
            brand_hint=hint,
        )

    @staticmethod
    def _money(raw: Any, scale: int) -> float | None:
        v = parse_price(raw)
        if v is None:
            return None
        return v / scale if scale > 1 else v

    @staticmethod
    def _brand_attr(p: dict[str, Any]) -> str | None:
        for attr in p.get("attributes") or []:
            if "brand" in str(attr.get("name", "")).lower():
                terms = attr.get("terms") or []
                if terms:
                    return str(terms[0].get("name") or "")
        return None
