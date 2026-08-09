"""Amazon.in search scraper.

Amazon splits a result title across two nodes: an <h2> carrying the brand and
a `s-line-clamp` anchor carrying the rest. Sponsored cards instead put the full
title in the h2's aria-label behind a "Sponsored Ad - " prefix. Both shapes are
handled; verified against live markup.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser, Node

from ..normalize import clean_text, is_target_diecast, parse_price
from .base import HttpSource, Item, looks_out_of_stock

CARD = 'div[data-component-type="s-search-result"]'
_SPONSORED = re.compile(r"^Sponsored Ad\s*-\s*", re.I)


class AmazonIN(HttpSource):
    name = "amazon_in"
    label = "Amazon.in"
    kind = "marketplace"

    BASE = "https://www.amazon.in"

    async def search(self, query: str) -> list[Item]:
        pages = int(self.cfg.source_opt(self.name, "max_pages", 2))
        out: list[Item] = []

        async with self.client(headers={"Referer": f"{self.BASE}/"}) as client:
            for page in range(1, pages + 1):
                url = f"{self.BASE}/s?k={quote_plus(query)}&page={page}"
                resp = await client.get(url)
                if resp.status_code != 200:
                    print(f"  [{self.name}] page {page} HTTP {resp.status_code}")
                    break

                html = resp.text
                if "Enter the characters you see" in html or "api-services-support@amazon.com" in html:
                    print(f"  [{self.name}] captcha wall hit on page {page}")
                    break

                cards = HTMLParser(html).css(CARD)
                if not cards:
                    break

                for card in cards:
                    item = self._parse(card)
                    if item:
                        out.append(item)

                if page < pages:
                    await self.pause()

        return out

    # ------------------------------------------------------------------
    def _parse(self, card: Node) -> Item | None:
        asin = card.attributes.get("data-asin")
        if not asin:
            return None

        title = self._title(card)
        if not title or not is_target_diecast(title):
            return None

        price_node = card.css_first(".a-price .a-offscreen")
        price = parse_price(price_node.text(strip=True)) if price_node else None
        if not self.keep(price):
            return None

        # A card with no price is either genuinely unavailable or a variation
        # parent ("See options") whose price depends on the colour you pick.
        # Only the first is out of stock; calling both out of stock would be
        # wrong, so availability keys off the explicit wording.
        in_stock = not looks_out_of_stock(card.text(separator=" ", strip=True))

        mrp_node = card.css_first(".a-text-price .a-offscreen")
        mrp = parse_price(mrp_node.text(strip=True)) if mrp_node else None

        rating = None
        if (r := card.css_first("span.a-icon-alt")) is not None:
            m = re.search(r"([\d.]+)\s+out of", r.text(strip=True))
            if m:
                rating = float(m.group(1))

        reviews = None
        if (rv := card.css_first('[data-cy="reviews-block"] span.s-underline-text')) is not None:
            reviews = self._count(rv.text(strip=True))

        img = card.css_first("img.s-image")

        return Item(
            source=self.name,
            source_sku=asin,
            region=self.region,
            raw_title=title,
            price=None if price is None else float(price),
            mrp=mrp,
            url=f"{self.BASE}/dp/{asin}",
            rating=rating,
            reviews=reviews,
            image_url=img.attributes.get("src") if img else None,
            in_stock=in_stock,
        )

    @staticmethod
    def _title(card: Node) -> str | None:
        h2 = card.css_first("h2")
        if h2 is None:
            return None

        aria = _SPONSORED.sub("", (h2.attributes.get("aria-label") or "").strip())
        brand = h2.text(separator=" ", strip=True)

        tail_node = card.css_first('a[class*="s-line-clamp"]')
        tail = tail_node.text(separator=" ", strip=True) if tail_node else ""

        if len(aria) > len(brand):
            return clean_text(aria)
        if tail and not tail.lower().startswith(brand.lower()):
            return clean_text(f"{brand} {tail}")
        return clean_text(tail or brand) or None

    @staticmethod
    def _count(text: str) -> int | None:
        """'(13.9K)' -> 13900"""
        m = re.search(r"([\d.,]+)\s*([KkMm])?", text or "")
        if not m:
            return None
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            return None
        mult = {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
        return int(n * mult)
