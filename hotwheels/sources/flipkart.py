"""Flipkart search scraper.

Flipkart ships obfuscated, frequently-rotated CSS class names (`_75nlfW`,
`tUxRFH`, ...), so selecting on them breaks within weeks. Instead we anchor on
the one stable signal — product links contain `/p/itm` — then walk up to the
nearest ancestor that also contains a rupee amount and parse that block's text.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from selectolax.parser import HTMLParser, Node

from ..normalize import clean_text, is_target_diecast, parse_price
from .base import BrowserHtmlSource, Item, looks_out_of_stock

_ITM = re.compile(r"/p/(itm[0-9a-z]+)", re.I)
_RUPEE = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")
_RATING = re.compile(r"^([1-5](?:\.\d)?)$")
_REVIEWS = re.compile(r"\(([\d,]+)\)")


class Flipkart(BrowserHtmlSource):
    name = "flipkart"
    label = "Flipkart"
    kind = "marketplace"

    BASE = "https://www.flipkart.com"

    async def search(self, query: str) -> list[Item]:
        pages = int(self.cfg.source_opt(self.name, "max_pages", 2))
        urls = [
            f"{self.BASE}/search?q={quote_plus(query)}&page={p}"
            for p in range(1, pages + 1)
        ]

        out: list[Item] = []
        seen: set[str] = set()

        for html in await self.fetch_pages(urls):
            if not html:
                continue
            anchors = [
                a for a in HTMLParser(html).css("a[href]")
                if "/p/itm" in (a.attributes.get("href") or "")
            ]
            for a in anchors:
                item = self._parse(a, seen)
                if item:
                    out.append(item)

        return out

    # ------------------------------------------------------------------
    def _parse(self, anchor: Node, seen: set[str]) -> Item | None:
        href = (anchor.attributes.get("href") or "").split("?")[0]
        m = _ITM.search(href)
        if not m:
            return None
        sku = m.group(1)
        if sku in seen:
            return None

        card = self._container(anchor)
        if card is None:
            return None

        parts = [p.strip() for p in card.text(separator="|", strip=True).split("|") if p.strip()]
        if not parts:
            return None

        title = self._full_title(card, parts[0])
        if not title or not is_target_diecast(title):
            return None

        blob = " ".join(parts)
        amounts = [parse_price(x) for x in _RUPEE.findall(blob)]
        amounts = [a for a in amounts if a]
        if not amounts:
            # Flipkart sometimes splits "₹" and the digits into sibling nodes.
            amounts = self._loose_amounts(parts)

        # Sold-out cards show no price at all. Keep them with price None so the
        # dashboard can say "Flipkart has it, but not right now".
        price = amounts[0] if amounts else None
        if not self.keep(price):
            return None
        mrp = next((a for a in amounts[1:] if price and a > price), None)

        in_stock = not looks_out_of_stock(blob)

        rating = None
        reviews = None
        for p in parts[1:6]:
            if rating is None and _RATING.match(p):
                rating = float(p)
            if reviews is None and (rm := _REVIEWS.fullmatch(p)):
                reviews = int(rm.group(1).replace(",", ""))

        seen.add(sku)

        return Item(
            source=self.name,
            source_sku=sku,
            region=self.region,
            raw_title=title,
            price=price,
            mrp=mrp,
            url=urljoin(self.BASE, href),
            rating=rating,
            reviews=reviews,
            image_url=self._image(card),
            in_stock=in_stock,
        )

    @staticmethod
    def _image(card: Node) -> str | None:
        """Real thumbnail URL, never Flipkart's lazy-load placeholder.

        Flipkart serves protocol-relative URLs ("//rukminim...") which break
        when embedded in a page served over http, so they are made absolute.
        """
        for img in card.css("img"):
            for attr in ("src", "data-src", "srcset"):
                raw = (img.attributes.get(attr) or "").split(" ")[0].strip()
                if not raw or "placeholder" in raw.lower():
                    continue
                if raw.startswith("//"):
                    raw = "https:" + raw
                return raw
        return None

    @staticmethod
    def _full_title(card: Node, visible: str) -> str:
        """Recover the untruncated title.

        Flipkart clips long names in the DOM with a literal ellipsis - "(5
        Monster T..." - which hides the pack size and wrecks cross-site
        matching. The complete string survives in the anchor's `title`
        attribute and the thumbnail's `alt`.
        """
        visible = clean_text(visible)
        if "..." not in visible and "…" not in visible:
            return visible

        for node, attr in (
            (card.css_first("a[title]"), "title"),
            (card.css_first("img[alt]"), "alt"),
        ):
            if node is None:
                continue
            full = clean_text(node.attributes.get(attr) or "")
            if len(full) > len(visible.rstrip(". ")):
                return full

        return visible

    @staticmethod
    def _container(anchor: Node) -> Node | None:
        """Nearest ancestor that is the whole product card.

        A price is the usual marker, but sold-out cards carry no price at all,
        so an out-of-stock phrase counts too. Failing both, fall back to the
        largest ancestor walked - dropping the card would lose exactly the
        unavailable listings we want to record.
        """
        node = anchor
        last = None
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            text = node.text()
            if "₹" in text or looks_out_of_stock(text):
                return node
            last = node
        return last

    @staticmethod
    def _loose_amounts(parts: list[str]) -> list[float]:
        """Recover prices when '₹' and digits land in separate text nodes."""
        out: list[float] = []
        for i, p in enumerate(parts):
            if p == "₹" and i + 1 < len(parts):
                v = parse_price(parts[i + 1])
                if v:
                    out.append(v)
        return out
