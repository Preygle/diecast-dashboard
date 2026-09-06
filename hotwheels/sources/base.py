"""Common contract every source adapter implements."""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Config

# Phrases the sites use for "you cannot buy this right now".
OOS_MARKERS = (
    "currently unavailable", "temporarily out of stock", "out of stock",
    "sold out", "notify me when available", "coming soon", "unavailable",
)


def looks_out_of_stock(text: str) -> bool:
    return any(m in (text or "").lower() for m in OOS_MARKERS)


CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    # Accept-Encoding is deliberately absent. httpx sets it from the codecs it
    # can actually decode; hardcoding "gzip, deflate, br" promised brotli on a
    # machine without the decoder, so servers sent brotli and every response
    # became unparseable - a 200 with the right content-type and a body we
    # could not read. Never advertise a capability the client may not have.
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


@dataclass
class Item:
    """One offer from one site.

    `price` is None when the site shows no price, which is how a sold-out
    listing usually renders. Those are kept and flagged out of stock rather
    than dropped, so the dashboard can show that an item exists here but
    cannot be bought right now.
    """

    source: str
    source_sku: str
    region: str
    raw_title: str
    price: float | None
    url: str | None = None
    mrp: float | None = None
    in_stock: bool = True
    rating: float | None = None
    reviews: int | None = None
    delivery_eta: str | None = None
    image_url: str | None = None
    # The store's own brand/vendor field. Specialty shops often omit the brand
    # from the title, so this is the only way to identify those listings.
    brand_hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Source:
    """Base adapter.

    Subclasses implement `search(query)` and yield Items already filtered to
    the configured price ceiling.
    """

    name: str = "base"
    label: str = "Base"
    kind: str = "marketplace"   # or "quickcom"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.region = cfg.region.pincode
        self.max_price = cfg.max_price

    async def search(self, query: str) -> list[Item]:
        raise NotImplementedError

    # How many of this source's queries may be in flight at once. Shops run
    # concurrently with each other already, so the slowest single shop sets
    # the length of a sweep - and that shop is slow because its queries were
    # strictly sequential. A small window cuts that without hammering anyone:
    # each request still waits `delay` before the next one on its own strand.
    query_concurrency: int = 4

    async def collect(self, queries: list[str]) -> list[Item]:
        """Run every query, de-duplicating by SKU within this source."""
        sem = asyncio.Semaphore(max(1, self.query_concurrency))

        async def one(q: str) -> list[Item]:
            async with sem:
                try:
                    found = await self.search(q)
                except Exception as exc:  # one bad query must not kill the source
                    print(f"  [{self.name}] query {q!r} failed: "
                          f"{type(exc).__name__}: {exc}")
                    found = []
                await self.pause()
                return found

        batches = await asyncio.gather(*(one(q) for q in queries))

        # Merged in query order, not completion order, so which listing wins a
        # duplicated SKU does not depend on how the network happened to behave.
        seen: dict[str, Item] = {}
        for found in batches:
            for item in found:
                seen.setdefault(item.source_sku, item)
        return list(seen.values())

    async def pause(self) -> None:
        await asyncio.sleep(self.cfg.delay + random.uniform(0, 0.7))

    def within_cap(self, price: float | None) -> bool:
        if price is None or price <= 0:
            return False
        return self.max_price is None or price <= self.max_price

    def keep(self, price: float | None) -> bool:
        """Whether to store a listing.

        A priced listing must respect the ceiling. An unpriced one is kept:
        no price means the site is not selling it right now, and knowing an
        item is listed-but-unavailable is the point of tracking stock.
        """
        return True if price is None else self.within_cap(price)


# What a browser sends for a JSON fetch. BASE_HEADERS describes a *document*
# navigation - `Accept: text/html`, `Sec-Fetch-Dest: document` - and sending
# that to a JSON endpoint invites the server to content-negotiate and hand
# back the HTML storefront instead. Shopify's edge does exactly that for
# datacentre clients: the same /products.json request returns 660KB of JSON
# with these headers and 37KB of HTML with the document ones.
JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class HttpSource(Source):
    """Adapter backed by plain HTTP requests."""

    def json_client(self, **kw: Any) -> httpx.AsyncClient:
        """A client for API endpoints. Use this for anything returning JSON."""
        headers = dict(JSON_HEADERS)
        headers.update(kw.pop("headers", {}))
        client = self.client(headers=headers, **kw)
        # Only meaningful on a document navigation.
        client.headers.pop("Sec-Fetch-User", None)
        client.headers.pop("Upgrade-Insecure-Requests", None)
        return client

    def client(self, **kw: Any) -> httpx.AsyncClient:
        headers = dict(BASE_HEADERS)
        headers.update(kw.pop("headers", {}))
        return httpx.AsyncClient(
            headers=headers,
            timeout=self.cfg.timeout,
            follow_redirects=True,
            http2=True,
            **kw,
        )


class BrowserHtmlSource(Source):
    """Adapter that needs a real browser's TLS handshake to get served HTML.

    Flipkart returns 403 to httpx even with byte-identical headers while
    answering curl and Chrome normally, so the block keys off the TLS/client
    fingerprint. Driving Chromium sidesteps it without adding an impersonation
    library, and the markup we parse is the same either way.
    """

    async def fetch_pages(self, urls: list[str]) -> list[str]:
        from playwright.async_api import async_playwright

        pages_html: list[str] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.cfg.headless, args=CHROME_ARGS)
            ctx = await browser.new_context(
                user_agent=UA, locale="en-IN", timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 900},
            )
            await ctx.add_init_script(STEALTH_JS)
            page = await ctx.new_page()
            # Only video and fonts are blocked. Images must be allowed through:
            # Flipkart lazy-loads thumbnails, so aborting image requests leaves
            # every <img> stuck on a placeholder URL and the dashboard ends up
            # with no product photos at all.
            await page.route(
                "**/*",
                lambda route: asyncio.ensure_future(
                    route.abort()
                    if route.request.resource_type in ("media", "font")
                    else route.continue_()
                ),
            )

            for url in urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=self.cfg.timeout * 1000)
                    await page.wait_for_timeout(2500)
                    pages_html.append(await page.content())
                except Exception as exc:
                    print(f"  [{self.name}] {url[:60]} failed: {type(exc).__name__}")
                    pages_html.append("")
                await self.pause()

            for closer in (ctx.close, browser.close):
                try:
                    await closer()
                except Exception:
                    pass

        return pages_html


@contextlib.asynccontextmanager
async def persistent_context(cfg: Config, *, headless: bool | None = None):
    """A Chromium profile that survives between runs.

    Quick-commerce catalogs are scoped to a delivery address the site keeps in
    session state, and Swiggy additionally puts an AWS WAF challenge in front of
    fresh headless sessions. Reusing one on-disk profile solves both: the
    address is set once by hand, and the WAF token it earns is carried forward.
    """
    from playwright.async_api import async_playwright

    profile = Path(cfg.raw.get("scrape", {}).get("profile_dir", ".browser_profile"))
    if not profile.is_absolute():
        profile = cfg.database.parent / profile
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=cfg.headless if headless is None else headless,
            args=CHROME_ARGS,
            user_agent=UA,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            geolocation={"latitude": cfg.region.lat, "longitude": cfg.region.lon},
            permissions=["geolocation"],
            viewport={"width": 1366, "height": 900},
        )
        await ctx.add_init_script(STEALTH_JS)
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()
