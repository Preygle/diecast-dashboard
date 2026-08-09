"""Quick-commerce adapters: Blinkit, Swiggy Instamart, Zepto.

None of these can be scraped over plain HTTP — direct calls return 403/404
because every catalog response is scoped to a delivery location carried in
session cookies and headers. So each adapter drives a real Chromium session,
plants the Chennai 600127 coordinates, and then reads the site's own internal
search XHRs as they fly past.

Their JSON shapes differ and change without notice, so instead of hard-coding
paths we walk each payload looking for objects that carry both a name-like and
a price-like key. That survives redesigns that would break a rigid parser.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote_plus

from ..normalize import clean_text, is_target_diecast, parse_price
from .base import Item, Source, persistent_context

# Key aliases seen across the three APIs.
NAME_KEYS = ("name", "display_name", "product_name", "title", "displayName", "productName")
PRICE_KEYS = (
    "price", "offer_price", "selling_price", "sellingPrice", "discountedSellingPrice",
    "offerPrice", "final_price", "discounted_price", "storePrice",
)
MRP_KEYS = ("mrp", "market_price", "strike_price", "originalPrice", "mrpPrice", "listPrice")
ID_KEYS = ("product_id", "productId", "id", "sku", "variant_id", "variantId", "item_id")
IMG_KEYS = ("image_url", "imageUrl", "image", "images", "product_image", "imagePath")
STOCK_KEYS = ("inventory", "in_stock", "available", "inStock", "availableQuantity", "stock")


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, "", [], {}):
            return obj[k]
    return None


def _walk(
    node: Any, parents: tuple[dict[str, Any], ...] = (), depth: int = 0
) -> Iterator[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]:
    """Yield every dict in a nested JSON structure, with its ancestor chain."""
    if depth > 14:
        return
    if isinstance(node, dict):
        yield node, parents
        for v in node.values():
            yield from _walk(v, parents + (node,), depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, parents, depth + 1)


def _nearby(
    obj: dict[str, Any], parents: tuple[dict[str, Any], ...], keys: tuple[str, ...]
) -> Any:
    """Find a value on the object, its direct children, or its close ancestors.

    Zepto splits a product across objects: `name` sits on the product while
    `sellingPrice` sits on the variant wrapper around it. Looking only inside a
    single dict finds names with no prices, so we widen the search by one level
    down and two up - close enough to stay within one product's subtree.
    """
    if (v := _first(obj, keys)) is not None:
        return v
    for child in obj.values():
        if isinstance(child, dict) and (v := _first(child, keys)) is not None:
            return v
    for ancestor in reversed(parents[-2:]):
        if (v := _first(ancestor, keys)) is not None:
            return v
        for child in ancestor.values():
            if isinstance(child, dict) and (v := _first(child, keys)) is not None:
                return v
    return None


# Headings like 'Showing results for "hot wheels"' otherwise pass the brand filter.
_NOT_A_PRODUCT = re.compile(
    r"^(showing|search)\s+results|^results\s+for|^did you mean", re.I
)

# Coordinates these apps put on their own API calls, used to confirm the
# catalogue really is for the configured area.
_LATLON = re.compile(r"lat(?:itude)?=(-?\d+\.\d+)&(?:lng|lon|longitude)=(-?\d+\.\d+)", re.I)

# ~0.5 degrees is roughly 55 km - generous enough for a metro's edge stores,
# tight enough to catch a different city.
LOCATION_TOLERANCE_DEG = 0.5


def _as_price(val: Any) -> float | None:
    """Handle plain numbers, strings, and nested {'value': N} shapes."""
    if isinstance(val, dict):
        for k in ("value", "amount", "price", "offer_price", "units"):
            if k in val:
                return _as_price(val[k])
        return None
    v = parse_price(val)
    if v is None:
        return None
    # Several of these APIs quote paise, not rupees.
    if v > 100_000:
        v = v / 100.0
    return v


def _as_image(val: Any) -> str | None:
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val:
        return _as_image(val[0])
    if isinstance(val, dict):
        for k in ("url", "src", "image_url", "path"):
            if k in val:
                return _as_image(val[k])
    return None


def _as_stock(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        return val.strip().lower() not in {"0", "false", "out_of_stock", "unavailable", "no"}
    return True


class QuickCommerce(Source):
    """Shared Playwright driver. Subclasses supply URLs and location plumbing."""

    kind = "quickcom"
    search_url: str = ""
    home_url: str = ""
    api_hints: tuple[str, ...] = ()
    eta: str | None = None

    async def search(self, query: str) -> list[Item]:
        items: dict[str, Item] = {}
        payloads: list[Any] = []
        body_text = ""
        seen_coords: list[tuple[float, float]] = []

        async with persistent_context(self.cfg) as ctx:
            await self.prime(ctx)
            page = await ctx.new_page()

            def on_request(req: Any) -> None:
                m = _LATLON.search(req.url)
                if m:
                    try:
                        seen_coords.append((float(m.group(1)), float(m.group(2))))
                    except ValueError:
                        pass

            page.on("request", on_request)

            async def on_response(resp: Any) -> None:
                url = resp.url
                if not any(h in url for h in self.api_hints):
                    return
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype:
                    return
                try:
                    payloads.append(await resp.json())
                except Exception:
                    with contextlib.suppress(Exception):
                        payloads.append(json.loads(await resp.text()))

            page.on("response", on_response)

            target = self.search_url.format(q=quote_plus(query))
            try:
                await page.goto(target, wait_until="domcontentloaded",
                                timeout=self.cfg.timeout * 1000)
                await page.wait_for_timeout(5000)
                for _ in range(3):
                    await page.mouse.wheel(0, 2200)
                    await page.wait_for_timeout(1800)
                with contextlib.suppress(Exception):
                    body_text = await page.inner_text("body")
            except Exception as exc:
                print(f"  [{self.name}] navigation issue: {type(exc).__name__}: {exc}")

        for payload in payloads:
            for item in self._harvest(payload):
                items.setdefault(item.source_sku, item)

        if items and not self._location_ok(seen_coords):
            # Better nothing than another city's prices labelled as yours.
            print(f"  [{self.name}] served a store far from "
                  f"{self.cfg.region.label} - discarding {len(items)} listing(s); "
                  f"run `python cli.py setup {self.name}`")
            return []

        if not items:
            self._diagnose(payloads, body_text)

        return list(items.values())

    def _location_ok(self, coords: list[tuple[float, float]]) -> bool:
        """Reject a catalogue served for somewhere else.

        Zepto accepts the browser's GPS coordinates and then calls its own API
        with a default store's coordinates anyway - observed answering Chennai
        requests with latitude 12.969/longitude 77.754, which is Bengaluru.
        Prices from the wrong city are worse than no prices, so any request
        carrying coordinates more than ~55 km away invalidates the run.
        """
        if not coords:
            return True  # nothing to check against; other guards still apply

        want = (self.cfg.region.lat, self.cfg.region.lon)
        return any(
            abs(lat - want[0]) <= LOCATION_TOLERANCE_DEG
            and abs(lon - want[1]) <= LOCATION_TOLERANCE_DEG
            for lat, lon in coords
        )

    def _diagnose(self, payloads: list[Any], body_text: str) -> None:
        """Say why nothing came back, instead of silently reporting zero."""
        low = (body_text or "").lower()
        if "something went wrong" in low or "are you a robot" in low:
            print(f"  [{self.name}] bot challenge blocked the page - "
                  f"run `python cli.py setup {self.name}` and solve it once")
        elif "select location" in low or "set delivery location" in low or not low:
            print(f"  [{self.name}] no delivery address set for this profile - "
                  f"run `python cli.py setup {self.name}`")
        elif not payloads:
            print(f"  [{self.name}] no catalog JSON captured")
        else:
            print(f"  [{self.name}] catalog returned no Hot Wheels under the cap")

    async def prime(self, ctx: Any) -> None:
        """Plant location hints before the first navigation."""
        return None

    async def setup(self) -> None:
        """Open a visible browser so a delivery address can be set by hand."""
        async with persistent_context(self.cfg, headless=False) as ctx:
            await self.prime(ctx)
            page = await ctx.new_page()
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=60_000)
            print(f"\n  {self.label} is open.")
            print(f"  Set the delivery address to {self.cfg.region.label} "
                  f"({self.cfg.region.pincode}), clear any bot check, then close the window.")
            # Block until the operator closes the browser.
            with contextlib.suppress(Exception):
                while len(ctx.pages) > 0:
                    await page.wait_for_timeout(1500)

    # ------------------------------------------------------------------
    def _harvest(self, payload: Any) -> Iterator[Item]:
        for obj, parents in _walk(payload):
            name = _first(obj, NAME_KEYS)
            if not isinstance(name, str):
                continue
            title = clean_text(name)
            if not title or _NOT_A_PRODUCT.search(title):
                continue
            if not is_target_diecast(title):
                continue

            sku = _first(obj, ID_KEYS)
            if sku is None:
                continue  # headings and banners carry no product id

            price = _as_price(_nearby(obj, parents, PRICE_KEYS))
            in_stock = _as_stock(_nearby(obj, parents, STOCK_KEYS))

            # Keep sold-out items, which these apps list with no price. But an
            # object with neither a price nor a stock signal is not a product
            # card at all - usually a recommendation stub - so it is skipped.
            if price is None and _nearby(obj, parents, STOCK_KEYS) is None:
                continue
            if not self.keep(price):
                continue

            yield Item(
                source=self.name,
                source_sku=str(sku),
                region=self.region,
                raw_title=title,
                price=None if price is None else float(price),
                mrp=_as_price(_nearby(obj, parents, MRP_KEYS)),
                url=self.product_url(obj, str(sku)),
                image_url=_as_image(_nearby(obj, parents, IMG_KEYS)),
                in_stock=in_stock,
                delivery_eta=self.eta,
            )

    def product_url(self, obj: dict[str, Any], sku: str) -> str | None:
        return None


class Blinkit(QuickCommerce):
    name = "blinkit"
    label = "Blinkit"
    search_url = "https://blinkit.com/s/?q={q}"
    home_url = "https://blinkit.com/"
    api_hints = ("/v1/layout/search", "/v2/search", "/layout/search", "/api/search")
    eta = "~10 min"

    async def prime(self, ctx: Any) -> None:
        lat, lon = self.cfg.region.lat, self.cfg.region.lon
        await ctx.add_cookies([
            {"name": n, "value": v, "domain": ".blinkit.com", "path": "/"}
            for n, v in [
                ("gr_1_lat", str(lat)),
                ("gr_1_lon", str(lon)),
                ("gr_1_deviceLatitude", str(lat)),
                ("gr_1_deviceLongitude", str(lon)),
                ("gr_1_locality", self.cfg.region.pincode),
            ]
        ])

    def product_url(self, obj: dict[str, Any], sku: str) -> str:
        return f"https://blinkit.com/prn/x/prid/{sku}"


class Instamart(QuickCommerce):
    name = "instamart"
    label = "Swiggy Instamart"
    search_url = "https://www.swiggy.com/instamart/search?custom_back=true&query={q}"
    home_url = "https://www.swiggy.com/instamart"
    api_hints = ("/api/instamart/search", "/instamart/search", "/api/instamart")
    eta = "~15 min"

    async def prime(self, ctx: Any) -> None:
        lat, lon = self.cfg.region.lat, self.cfg.region.lon
        payload = quote_plus(json.dumps({
            "lat": lat, "lng": lon,
            "address": f"{self.cfg.region.label}, {self.cfg.region.city} {self.cfg.region.pincode}",
        }))
        await ctx.add_cookies([
            {"name": "userLocation", "value": payload, "domain": ".swiggy.com", "path": "/"},
        ])

    def product_url(self, obj: dict[str, Any], sku: str) -> str:
        return f"https://www.swiggy.com/instamart/item/{sku}"


class Zepto(QuickCommerce):
    name = "zepto"
    label = "Zepto"
    # zeptonow.com 301s to zepto.com; going straight there avoids losing the
    # first search XHR to the redirect.
    search_url = "https://www.zepto.com/search?query={q}"
    home_url = "https://www.zepto.com/"
    api_hints = ("user-search-service/api/v3/search", "/api/v3/search", "/api/v2/search")
    eta = "~10 min"

    def product_url(self, obj: dict[str, Any], sku: str) -> str:
        return f"https://www.zepto.com/pn/x/pvid/{sku}"
