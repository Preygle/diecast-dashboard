"""Delivery region: where the quick-commerce prices are quoted for.

Blinkit, Instamart and Zepto price and stock per delivery address, so the
region is not cosmetic - it decides what the catalogue even contains. This
module resolves a city name, a pincode, or raw coordinates into a region, and
writes it back to config.yaml so every later scrape uses it.

Locations are bundled for the cities these apps actually serve. Anything else
falls back to an online postcode lookup, which needs a network connection but
covers every Indian pincode.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG, Region

# label -> (pincode, lat, lon, state). Coordinates are city-centre; for a
# specific neighbourhood pass --lat/--lon, since a quick-commerce catalogue can
# differ across one city.
CITIES: dict[str, tuple[str, float, float, str]] = {
    "Chennai":            ("600001", 13.0827, 80.2707, "Tamil Nadu"),
    "VIT Chennai":        ("600127", 12.8406, 80.1534, "Tamil Nadu"),
    "Coimbatore":         ("641001", 11.0168, 76.9558, "Tamil Nadu"),
    "Madurai":            ("625001", 9.9252, 78.1198, "Tamil Nadu"),
    "Bengaluru":          ("560001", 12.9716, 77.5946, "Karnataka"),
    "Mysuru":             ("570001", 12.2958, 76.6394, "Karnataka"),
    "Mangaluru":          ("575001", 12.9141, 74.8560, "Karnataka"),
    "Hyderabad":          ("500001", 17.3850, 78.4867, "Telangana"),
    "Warangal":           ("506002", 17.9689, 79.5941, "Telangana"),
    "Vijayawada":         ("520001", 16.5062, 80.6480, "Andhra Pradesh"),
    "Visakhapatnam":      ("530001", 17.6868, 83.2185, "Andhra Pradesh"),
    "Kochi":              ("682001", 9.9312, 76.2673, "Kerala"),
    "Thiruvananthapuram": ("695001", 8.5241, 76.9366, "Kerala"),
    "Mumbai":             ("400001", 19.0760, 72.8777, "Maharashtra"),
    "Navi Mumbai":        ("400703", 19.0330, 73.0297, "Maharashtra"),
    "Thane":              ("400601", 19.2183, 72.9781, "Maharashtra"),
    "Pune":               ("411001", 18.5204, 73.8567, "Maharashtra"),
    "Nagpur":             ("440001", 21.1458, 79.0882, "Maharashtra"),
    "Nashik":             ("422001", 19.9975, 73.7898, "Maharashtra"),
    "Delhi":              ("110001", 28.6139, 77.2090, "Delhi"),
    "New Delhi":          ("110011", 28.6139, 77.2090, "Delhi"),
    "Gurugram":           ("122001", 28.4595, 77.0266, "Haryana"),
    "Noida":              ("201301", 28.5355, 77.3910, "Uttar Pradesh"),
    "Ghaziabad":          ("201001", 28.6692, 77.4538, "Uttar Pradesh"),
    "Faridabad":          ("121001", 28.4089, 77.3178, "Haryana"),
    "Lucknow":            ("226001", 26.8467, 80.9462, "Uttar Pradesh"),
    "Kanpur":             ("208001", 26.4499, 80.3319, "Uttar Pradesh"),
    "Varanasi":           ("221001", 25.3176, 82.9739, "Uttar Pradesh"),
    "Agra":               ("282001", 27.1767, 78.0081, "Uttar Pradesh"),
    "Jaipur":             ("302001", 26.9124, 75.7873, "Rajasthan"),
    "Jodhpur":            ("342001", 26.2389, 73.0243, "Rajasthan"),
    "Udaipur":            ("313001", 24.5854, 73.7125, "Rajasthan"),
    "Ahmedabad":          ("380001", 23.0225, 72.5714, "Gujarat"),
    "Surat":              ("395003", 21.1702, 72.8311, "Gujarat"),
    "Vadodara":           ("390001", 22.3072, 73.1812, "Gujarat"),
    "Rajkot":             ("360001", 22.3039, 70.8022, "Gujarat"),
    "Kolkata":            ("700001", 22.5726, 88.3639, "West Bengal"),
    "Howrah":             ("711101", 22.5958, 88.2636, "West Bengal"),
    "Siliguri":           ("734001", 26.7271, 88.3953, "West Bengal"),
    "Bhubaneswar":        ("751001", 20.2961, 85.8245, "Odisha"),
    "Patna":              ("800001", 25.5941, 85.1376, "Bihar"),
    "Ranchi":             ("834001", 23.3441, 85.3096, "Jharkhand"),
    "Raipur":             ("492001", 21.2514, 81.6296, "Chhattisgarh"),
    "Bhopal":             ("462001", 23.2599, 77.4126, "Madhya Pradesh"),
    "Indore":             ("452001", 22.7196, 75.8577, "Madhya Pradesh"),
    "Chandigarh":         ("160001", 30.7333, 76.7794, "Chandigarh"),
    "Ludhiana":           ("141001", 30.9010, 75.8573, "Punjab"),
    "Amritsar":           ("143001", 31.6340, 74.8723, "Punjab"),
    "Dehradun":           ("248001", 30.3165, 78.0322, "Uttarakhand"),
    "Guwahati":           ("781001", 26.1445, 91.7362, "Assam"),
    "Goa":                ("403001", 15.4909, 73.8278, "Goa"),
}

_PIN_RE = re.compile(r"^\d{6}$")


def known_regions() -> list[dict[str, Any]]:
    """Every bundled city, for a picker."""
    return [
        {"label": name, "pincode": pin, "lat": lat, "lon": lon, "state": state}
        for name, (pin, lat, lon, state) in CITIES.items()
    ]


def _from_city(name: str) -> Region | None:
    for label, (pin, lat, lon, _state) in CITIES.items():
        if label.lower() == name.strip().lower():
            return Region(label=label, pincode=pin, city=label.split()[-1], lat=lat, lon=lon)
    return None


def _from_pincode(pin: str) -> Region | None:
    for label, (p, lat, lon, _state) in CITIES.items():
        if p == pin:
            return Region(label=label, pincode=pin, city=label, lat=lat, lon=lon)
    return None


def _geocode(pin: str, timeout: float = 15.0) -> Region | None:
    """Look an unbundled pincode up via OpenStreetMap.

    Nominatim asks for a descriptive User-Agent and light usage; this runs once
    when you change region, not per scrape.
    """
    import httpx

    url = "https://nominatim.openstreetmap.org/search"
    params = {"postalcode": pin, "country": "India", "format": "json", "limit": 1}
    headers = {"User-Agent": "hotwheels-price-dashboard/1.0 (region lookup)"}
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return None
        hits = r.json()
    except Exception:
        return None
    if not hits:
        return None

    hit = hits[0]
    name = (hit.get("display_name") or pin).split(",")[0].strip()
    return Region(
        label=f"{name} {pin}", pincode=pin, city=name,
        lat=float(hit["lat"]), lon=float(hit["lon"]),
    )


def resolve(query: str, *, online: bool = True) -> Region:
    """Turn a city name or a 6-digit pincode into a Region.

    Raises ValueError with a usable message rather than guessing, because a
    wrong location silently produces another city's prices.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("Give a city name or a 6-digit pincode.")

    if _PIN_RE.match(q):
        region = _from_pincode(q) or (_geocode(q) if online else None)
        if region:
            return region
        raise ValueError(
            f"Could not place pincode {q}. Pass coordinates directly:\n"
            f"  python cli.py region set --pincode {q} --lat <lat> --lon <lon> --label <name>"
        )

    region = _from_city(q)
    if region:
        return region

    near = [c for c in CITIES if q.lower() in c.lower()]
    hint = f" Did you mean: {', '.join(near[:5])}?" if near else ""
    raise ValueError(f"Unknown city {q!r}.{hint} Run `python cli.py region list`.")


def save(region: Region, config_path: str | Path | None = None) -> Path:
    """Write the region into config.yaml, preserving everything else.

    The file is rewritten line by line rather than re-dumped, so the comments
    explaining every other setting survive.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    text = path.read_text(encoding="utf-8")

    fields = {
        "label": f'"{region.label}"',
        "pincode": f'"{region.pincode}"',
        "city": f'"{region.city}"',
        "lat": f"{region.lat}",
        "lon": f"{region.lon}",
    }

    out, in_region, done = [], False, set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("region:"):
            in_region = True
            out.append(line)
            continue
        if in_region:
            # The region block ends at the next top-level key.
            if stripped and not line.startswith((" ", "\t")) and not stripped.startswith("#"):
                for key, val in fields.items():
                    if key not in done:
                        out.append(f"  {key}: {val}")
                in_region = False
            else:
                m = re.match(r"^(\s+)(\w+):", line)
                if m and m.group(2) in fields:
                    key = m.group(2)
                    out.append(f"{m.group(1)}{key}: {fields[key]}")
                    done.add(key)
                    continue
        out.append(line)

    if in_region:
        for key, val in fields.items():
            if key not in done:
                out.append(f"  {key}: {val}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def describe(region: Region) -> str:
    return f"{region.label} ({region.pincode}) at {region.lat}, {region.lon}"


def as_dict(region: Region) -> dict[str, Any]:
    return asdict(region)


__all__ = ["CITIES", "known_regions", "resolve", "save", "describe", "as_dict"]
