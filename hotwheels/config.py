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
    max_price: float
    currency: str
    queries: list[str]
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
        max_price=float(data.get("max_price", 2000)),
        currency=data.get("currency", "INR"),
        queries=list(data.get("queries", ["hot wheels"])),
        sources=data.get("sources", {}),
        delay=float(scrape.get("delay", 1.5)),
        timeout=float(scrape.get("timeout", 30)),
        match_threshold=int(scrape.get("match_threshold", 87)),
        headless=bool(scrape.get("headless", True)),
        database=db,
        raw=data,
    )
