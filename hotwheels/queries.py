"""Read-side queries powering the dashboard."""

from __future__ import annotations

import sqlite3
from typing import Any

# Price bands offered as one-click tags. Open-ended at the top so the last
# band always reaches the configured ceiling.
PRICE_BANDS: list[tuple[str, float, float]] = [
    ("Under 250", 0, 250),
    ("250-500", 250, 500),
    ("500-1000", 500, 1000),
    ("1000-1500", 1000, 1500),
    ("1500+", 1500, 10_000),
]

# Pack-size tags. 6+ collapses the long tail of 8/10/20-car boxes.
PACK_BANDS: list[tuple[str, int, int]] = [
    ("Single", 1, 1),
    ("2-3 pack", 2, 3),
    ("4-5 pack", 4, 5),
    ("6+ pack", 6, 99),
]


def _band(bands: list[tuple[str, Any, Any]], label: str | None) -> tuple[Any, Any] | None:
    if not label:
        return None
    for name, lo, hi in bands:
        if name == label:
            return lo, hi
    return None


def catalog(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    sources: list[str] | None = None,
    brands: list[str] | None = None,
    series: str | None = None,
    realism: str | None = None,
    packs: list[str] | None = None,
    price_band: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    in_stock_only: bool = False,
    oos_only: bool = False,
    sets_only: bool = False,
    region: str | None = None,
    sort: str = "price",
    limit: int = 60,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Products with their per-site offers, cheapest offer first."""
    where = ["1=1"]
    params: list[Any] = []

    # Quick-commerce prices are quoted per delivery area, so listings from
    # another region must never be mixed into these results.
    if region:
        where.append("l.region = ?")
        params.append(region)

    if q:
        where.append("(p.title LIKE ? OR l.raw_title LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if sources:
        where.append(f"l.source IN ({','.join('?' * len(sources))})")
        params += sources
    if brands:
        where.append(f"p.brand IN ({','.join('?' * len(brands))})")
        params += brands
    if series:
        where.append("p.series = ?")
        params.append(series)
    if realism:
        if realism == "Mixed":
            # Packs and track sets, where the question does not apply.
            where.append("p.realism IS NULL")
        else:
            where.append("p.realism = ?")
            params.append(realism)
    if packs:
        clauses = []
        for label in packs:
            rng = _band(PACK_BANDS, label)
            if rng:
                clauses.append("(p.pack_size BETWEEN ? AND ?)")
                params += [rng[0], rng[1]]
        if clauses:
            where.append("(" + " OR ".join(clauses) + ")")
    if (band := _band(PRICE_BANDS, price_band)) is not None:
        where.append("l.price >= ? AND l.price < ?")
        params += [band[0], band[1]]
    if min_price is not None:
        where.append("l.price >= ?")
        params.append(min_price)
    if max_price is not None:
        # An unpriced (sold-out) listing has no price to compare, so a ceiling
        # must not silently exclude it.
        where.append("(l.price <= ? OR l.price IS NULL)")
        params.append(max_price)
    if in_stock_only:
        where.append("l.in_stock = 1 AND l.price IS NOT NULL")
    if oos_only:
        where.append("l.in_stock = 0")
    if sets_only:
        where.append("p.is_set = 1")

    sql = f"""
        SELECT p.id, p.title, p.brand, p.series, p.realism,
               p.pack_size, p.is_set, p.image_url,
               -- MIN/MAX skip NULLs, so an unpriced sold-out offer never
               -- becomes the "best price".
               MIN(l.price) AS best_price,
               MAX(l.price) AS worst_price,
               COUNT(DISTINCT l.source) AS source_count,
               SUM(CASE WHEN l.in_stock = 1 AND l.price IS NOT NULL THEN 1 ELSE 0 END)
                   AS buyable_count,
               SUM(CASE WHEN l.in_stock = 0 THEN 1 ELSE 0 END) AS oos_count,
               MAX(l.rating) AS rating,
               MAX(l.reviews) AS reviews
          FROM products p
          JOIN listings l ON l.product_id = p.id
         WHERE {' AND '.join(where)}
      GROUP BY p.id
    """

    # Price sorts lead with what you can actually buy. A collector shop lists
    # every car it ever stocked, so a naive "cheapest first" fills the page
    # with sold-out Rs 40 listings and buries every purchasable item.
    # Unpriced products sort last too, rather than first as SQLite's NULL
    # ordering would put them.
    order = {
        "price": "buyable_count = 0, best_price IS NULL, best_price ASC",
        "price_desc": "buyable_count = 0, best_price IS NULL, best_price DESC",
        "savings": "(worst_price - best_price) DESC",
        "sources": "source_count DESC, best_price ASC",
        "rating": "rating DESC NULLS LAST, best_price ASC",
        "title": "p.title ASC",
    }.get(sort, "best_price ASC")

    # One extra row is fetched so the caller can tell whether more exist
    # without running a second COUNT query over the same filters.
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    params += [limit + 1, offset]

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return []
    rows[0]["_has_more"] = has_more

    ids = [r["id"] for r in rows]
    offer_sql = f"""
        SELECT product_id, source, price, mrp, in_stock, url, delivery_eta,
               raw_title, rating, reviews, scraped_at
          FROM listings
         WHERE product_id IN ({','.join('?' * len(ids))})
      ORDER BY price ASC
    """
    offers: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(offer_sql, ids).fetchall():
        offers.setdefault(r["product_id"], []).append(dict(r))

    for row in rows:
        row["offers"] = offers.get(row["id"], [])
        if row["best_price"] is not None and row["worst_price"] is not None:
            row["savings"] = round(row["worst_price"] - row["best_price"], 2)
        else:
            row["savings"] = 0
    return rows


def _region_clause(region: str | None, alias: str = "l") -> tuple[str, list[Any]]:
    """SQL fragment scoping a query to one delivery region.

    Returned as (sql, params) so callers can splice it into either a WHERE or
    an AND position without string-guessing.
    """
    if not region:
        return "", []
    return f" AND {alias}.region = ?", [region]


def stats(conn: sqlite3.Connection, region: str | None = None) -> dict[str, Any]:
    rc, rp = _region_clause(region)
    one = lambda sql, extra=(): conn.execute(sql, list(extra)).fetchone()[0]  # noqa: E731

    per_source = [
        dict(r) for r in conn.execute(
            f"""SELECT source, COUNT(*) AS listings, MIN(price) AS cheapest,
                       ROUND(AVG(price), 2) AS average,
                       SUM(CASE WHEN in_stock = 0 THEN 1 ELSE 0 END) AS out_of_stock,
                       MAX(scraped_at) AS last_scraped
                  FROM listings l WHERE 1=1 {rc}
              GROUP BY source ORDER BY listings DESC""", rp
        ).fetchall()
    ]
    return {
        "products": one(
            f"""SELECT COUNT(DISTINCT p.id) FROM products p
                  JOIN listings l ON l.product_id = p.id WHERE 1=1 {rc}""", rp),
        "listings": one(f"SELECT COUNT(*) FROM listings l WHERE 1=1 {rc}", rp),
        "sources": one(f"SELECT COUNT(DISTINCT source) FROM listings l WHERE 1=1 {rc}", rp),
        "cheapest": one(
            f"""SELECT COALESCE(MIN(price), 0) FROM listings l
                 WHERE in_stock = 1 {rc}""", rp),
        "out_of_stock": one(
            f"SELECT COUNT(*) FROM listings l WHERE in_stock = 0 {rc}", rp),
        "last_scraped": one(
            f"SELECT COALESCE(MAX(scraped_at), '') FROM listings l WHERE 1=1 {rc}", rp),
        "per_source": per_source,
        "regions": [
            dict(r) for r in conn.execute(
                """SELECT region, COUNT(*) AS n FROM listings
                 GROUP BY region ORDER BY n DESC"""
            ).fetchall()
        ],
    }


def facets(conn: sqlite3.Connection, region: str | None = None) -> dict[str, Any]:
    """Counts behind every tag, so the UI never offers a filter that finds nothing."""
    rc, rp = _region_clause(region)
    join = f"FROM products p JOIN listings l ON l.product_id = p.id WHERE 1=1 {rc}"

    def rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, params or []).fetchall()]

    def count(sql: str, extra: list[Any]) -> int:
        return conn.execute(sql, extra).fetchone()["c"]

    brands = rows(
        f"""SELECT COALESCE(p.brand, 'Other') AS name, COUNT(DISTINCT p.id) AS n
            {join} GROUP BY COALESCE(p.brand, 'Other') ORDER BY n DESC""", rp)

    platforms = rows(
        f"""SELECT l.source AS name, COUNT(DISTINCT l.product_id) AS n
              FROM listings l WHERE 1=1 {rc}
          GROUP BY l.source ORDER BY n DESC""", rp)

    packs = [
        {"name": label,
         "n": count(f"SELECT COUNT(DISTINCT p.id) c {join} AND p.pack_size BETWEEN ? AND ?",
                    rp + [lo, hi])}
        for label, lo, hi in PACK_BANDS
    ]

    prices = [
        {"name": label,
         "n": count(f"SELECT COUNT(DISTINCT p.id) c {join} AND l.price >= ? AND l.price < ?",
                    rp + [lo, hi])}
        for label, lo, hi in PRICE_BANDS
    ]

    series = rows(
        f"""SELECT p.series AS name, COUNT(DISTINCT p.id) AS n
            {join} AND p.series IS NOT NULL
         GROUP BY p.series ORDER BY n DESC""", rp)

    realism = rows(
        f"""SELECT COALESCE(p.realism, 'Mixed') AS name, COUNT(DISTINCT p.id) AS n
            {join} GROUP BY COALESCE(p.realism, 'Mixed') ORDER BY n DESC""", rp)

    availability = [
        {"name": "In stock",
         "n": count(f"""SELECT COUNT(DISTINCT l.product_id) c FROM listings l
                         WHERE l.in_stock = 1 AND l.price IS NOT NULL {rc}""", rp)},
        {"name": "Out of stock",
         "n": count(f"""SELECT COUNT(DISTINCT l.product_id) c FROM listings l
                         WHERE l.in_stock = 0 {rc}""", rp)},
    ]

    return {
        "brands": [b for b in brands if b["n"]],
        "realism": [r for r in realism if r["n"]],
        "platforms": platforms,
        "packs": [p for p in packs if p["n"]],
        "prices": [p for p in prices if p["n"]],
        "series": series,
        "availability": [a for a in availability if a["n"]],
    }


def history(conn: sqlite3.Connection, product_id: int) -> list[dict[str, Any]]:
    return [
        dict(r) for r in conn.execute(
            """SELECT l.source, h.price, h.in_stock, h.ts
                 FROM price_history h
                 JOIN listings l ON l.id = h.listing_id
                WHERE l.product_id = ?
             ORDER BY h.ts ASC""",
            (product_id,),
        ).fetchall()
    ]


def deals(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Biggest gaps between the cheapest and dearest site for the same item."""
    return [
        dict(r) for r in conn.execute(
            """SELECT p.id, p.title, p.image_url,
                      MIN(l.price) AS best_price, MAX(l.price) AS worst_price,
                      COUNT(DISTINCT l.source) AS source_count
                 FROM products p JOIN listings l ON l.product_id = p.id
             GROUP BY p.id
               HAVING source_count > 1 AND worst_price > best_price
             ORDER BY (worst_price - best_price) DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]


# --------------------------------------------------------------------------
# watchlist
# --------------------------------------------------------------------------

def watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM watchlist WHERE active = 1 ORDER BY created_at DESC"
    ).fetchall()]

    for w in rows:
        matches = conn.execute(
            """SELECT p.id, p.title, MIN(l.price) AS best_price, l.source, l.url
                 FROM products p JOIN listings l ON l.product_id = p.id
                WHERE p.title LIKE ?
             GROUP BY p.id ORDER BY best_price ASC LIMIT 5""",
            (f"%{w['query']}%",),
        ).fetchall()
        w["matches"] = [dict(m) for m in matches]
        best = matches[0]["best_price"] if matches else None
        w["best_price"] = best
        w["hit"] = bool(w["target_price"] and best and best <= w["target_price"])
    return rows


# --------------------------------------------------------------------------
# markup guard
# --------------------------------------------------------------------------
#
# What an item *should* cost cannot be inferred from what shops charge for it.
# Two attempts failed on real data:
#
#   stated MRP   372 of 374 listings sit at or under their own, because shops
#                set the MRP field to whatever they are charging.
#   the median   of Hot Wheels mainline singles was Rs 179 across four shops
#                and Rs 499 across seventeen. Adding shops that sell above MRP
#                moved it, because a median measures the market, not the price.
#                The mode moved too: Rs 499 appears 102 times, Rs 179 only 62.
#
# A contaminated sample stays contaminated however it is averaged. So MRP is
# declared in config instead - it is a printed number, known to the buyer, and
# it does not drift. A class with no declared MRP falls back to a low
# percentile of live prices, which is a guess and is documented as one.

# Below this many comparable listings even the fallback is noise, and nothing
# is judged - a thin class must not produce false rejections.
MIN_SAMPLE = 8

# Percentile used when no MRP is declared. Low, because legitimate MRP-priced
# sellers are the cheap tail of a market that mostly sells above MRP.
FALLBACK_PCT = 0.10


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


def declared_mrp(cfg_mrp: dict[str, Any], brand: str | None,
                 series: str | None) -> float | None:
    """The printed MRP for one car of this kind, if config states it.

    `Brand/Series` wins over `Brand`, so a Premium is judged against Premium
    money rather than against a basic mainline.
    """
    if not cfg_mrp or not brand:
        return None
    if series:
        exact = cfg_mrp.get(f"{brand}/{series}")
        if exact:
            return float(exact)
    plain = cfg_mrp.get(brand)
    return float(plain) if plain else None


def price_baselines(conn: sqlite3.Connection, region: str | None = None,
                    cfg_mrp: dict[str, Any] | None = None) -> dict[tuple, float]:
    """What one of each (brand, series, pack_size) ought to cost.

    A declared MRP is multiplied by the pack size: diecast multipacks price at
    roughly the single rate per car, so a five-pack of Rs 179 cars comes to
    about Rs 895 - which is what they sell for.
    """
    rc, rp = _region_clause(region)
    rows = conn.execute(
        f"""SELECT p.brand, p.series, p.pack_size, l.price
              FROM products p JOIN listings l ON l.product_id = p.id
             WHERE l.price IS NOT NULL AND l.price > 0 AND l.in_stock = 1
               AND p.brand IS NOT NULL {rc}""", rp
    ).fetchall()

    buckets: dict[tuple, list[float]] = {}
    for r in rows:
        buckets.setdefault((r["brand"], r["series"], r["pack_size"]), []).append(r["price"])

    out: dict[tuple, float] = {}
    for key, prices in buckets.items():
        brand, series, pack = key
        mrp = declared_mrp(cfg_mrp or {}, brand, series)
        if mrp is not None:
            out[key] = mrp * max(int(pack or 1), 1)
        elif len(prices) >= MIN_SAMPLE:
            out[key] = _percentile(prices, FALLBACK_PCT)
    return out


def markup_of(price: float | None, brand: str | None, series: str | None,
              pack_size: int | None,
              baselines: dict[tuple, float]) -> float | None:
    """How far above MRP this price sits, or None when unjudgeable.

    0.0 means at or below MRP; 0.25 means a quarter over.
    """
    if price is None or price <= 0:
        return None
    base = baselines.get((brand, series, pack_size))
    if not base:
        return None
    return max(0.0, price / base - 1.0)
