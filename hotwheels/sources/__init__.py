"""Source registry.

Five sources are bespoke because each site needed its own defeat: Amazon and
Flipkart parse HTML, the three quick-commerce apps drive a browser. Every other
shop is built from config through a generic Shopify or WooCommerce adapter, so
adding a store is a line of YAML rather than a new scraper.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from .amazon_in import AmazonIN
from .base import Item, Source
from .flipkart import Flipkart
from .quickcom import Blinkit, Instamart, Zepto
from .shopify import ShopifyStore
from .woocommerce import WooStore

# Sources with a fixed identity and a dedicated adapter.
REGISTRY: dict[str, type[Source]] = {
    AmazonIN.name: AmazonIN,
    Flipkart.name: Flipkart,
    Blinkit.name: Blinkit,
    Instamart.name: Instamart,
    Zepto.name: Zepto,
}

# Sources that need a real browser session: quick-commerce for location
# cookies and JS-rendered catalogs, Flipkart because it fingerprints TLS
# clients and 403s plain HTTP requests.
BROWSER_SOURCES = {Flipkart.name, Blinkit.name, Instamart.name, Zepto.name}

PLATFORMS = {"shopify": ShopifyStore, "woocommerce": WooStore}


def store_definitions(cfg: Config) -> list[dict[str, Any]]:
    """Every configured storefront, regardless of platform."""
    out: list[dict[str, Any]] = []
    for platform in PLATFORMS:
        block = cfg.sources.get(platform) or {}
        if not block.get("enabled", False):
            continue
        for store in block.get("stores", []) or []:
            if store.get("enabled", True) is False:
                continue
            out.append({**store, "platform": platform, "_defaults": block})
    return out


def build_sources(cfg: Config, only: list[str] | None = None) -> list[Source]:
    """Instantiate every enabled source, bespoke and config-driven alike."""
    sources: list[Source] = []

    for name, cls in REGISTRY.items():
        if cfg.source_enabled(name):
            sources.append(cls(cfg))

    for store in store_definitions(cfg):
        cls = PLATFORMS[store["platform"]]
        defaults = store.get("_defaults", {})
        kwargs: dict[str, Any] = {
            "name": store["name"],
            "label": store.get("label", store["name"]),
            "domain": store["domain"],
        }
        if store["platform"] == "shopify":
            kwargs["mode"] = store.get("mode", defaults.get("mode", "catalog"))
            kwargs["max_pages"] = int(
                store.get("max_pages", defaults.get("max_pages", 6))
            )
        else:
            kwargs["per_page"] = int(
                store.get("per_page", defaults.get("per_page", 50))
            )
        sources.append(cls(cfg, **kwargs))

    if only:
        sources = [s for s in sources if s.name in only]
    return sources


def source_labels(cfg: Config) -> dict[str, str]:
    """name -> display label, for the dashboard."""
    return {s.name: s.label for s in build_sources(cfg)}


__all__ = [
    "Item", "Source", "REGISTRY", "BROWSER_SOURCES", "PLATFORMS",
    "build_sources", "source_labels", "store_definitions",
    "AmazonIN", "Flipkart", "Blinkit", "Instamart", "Zepto",
    "ShopifyStore", "WooStore",
]
