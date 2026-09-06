"""Config loading. One YAML file drives region, price cap and source toggles."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class Region:
    label: str
    pincode: str
    city: str
    lat: float
    lon: float


@dataclass
class Config:
    region: Region
    # None means no ceiling: every price is kept, however dear.
    max_price: float | None
    currency: str
    exclude_fantasy: bool
    # None disables the guard entirely.
    max_markup: float | None
    # Printed MRP per single car, keyed 'Brand' or 'Brand/Series'.
    mrp: dict[str, float]
    queries: list[str]
    fast_queries: list[str]
    fast_sources: list[str]
    sources: dict[str, dict[str, Any]]
    delay: float
    timeout: float
    match_threshold: int
    headless: bool
    database: Path
    raw: dict[str, Any] = field(default_factory=dict)

    def source_enabled(self, name: str) -> bool:
        return bool(self.sources.get(name, {}).get("enabled", False))

    def source_opt(self, name: str, key: str, default: Any = None) -> Any:
        return self.sources.get(name, {}).get(key, default)


def load(path: str | os.PathLike[str] | None = None) -> Config:
    # Environment overrides let the same image run with a mounted data volume
    # or an alternate config without editing files inside the container.
    cfg_path = Path(path or os.environ.get("HOTWHEELS_CONFIG") or DEFAULT_CONFIG)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    r = data["region"]
    scrape = data.get("scrape", {})
    db = Path(os.environ.get("HOTWHEELS_DB") or data.get("database", "hotwheels.db"))
    if not db.is_absolute():
        db = cfg_path.parent / db
    db.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        region=Region(
            label=r.get("label", r.get("city", "")),
            pincode=str(r["pincode"]),
            city=r.get("city", ""),
            lat=float(r["lat"]),
            lon=float(r["lon"]),
        ),
        # `max_price: null` (or 0, or absent) removes the ceiling entirely.
        max_price=(float(raw_cap) if (raw_cap := data.get("max_price")) else None),
        currency=data.get("currency", "INR"),
        exclude_fantasy=bool(data.get("exclude_fantasy", False)),
        max_markup=(float(mk) if (mk := data.get("max_markup")) is not None else None),
        mrp={str(k): float(v) for k, v in (data.get("mrp") or {}).items()},
        queries=list(data.get("queries", ["hot wheels"])),
        # Falling back to the full list keeps a config without this key working.
        fast_queries=list(data.get("fast_queries")
                          or data.get("queries", ["hot wheels"])),
        fast_sources=list(data.get("fast_sources") or []),
        sources=data.get("sources", {}),
        delay=float(scrape.get("delay", 1.5)),
        timeout=float(scrape.get("timeout", 30)),
        match_threshold=int(scrape.get("match_threshold", 87)),
        headless=bool(scrape.get("headless", True)),
        database=db,
        raw=data,
    )
