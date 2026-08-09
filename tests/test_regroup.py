"""Regroup must never destroy listings.

listings.product_id is ON DELETE CASCADE, so clearing `products` to rebuild
grouping takes every listing and its price history with it. This guards that.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import config, db, pipeline  # noqa: E402


def main() -> int:
    cfg = config.load()
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    cfg.database = tmp

    titles = [
        ("amazon_in", "a1", "Hot Wheels Basic Car 5-Pack, Multicolor, Set of 5 Toy Cars 1:64"),
        ("flipkart",  "f1", "HOT WHEELS 5 Car Pack Assortment (Multicolor, Pack of 5)"),
        ("blinkit",   "b1", "Hot Wheels 5 Car Pack"),
        ("amazon_in", "a2", "Hot Wheels Monster Trucks Glow in the Dark"),
    ]

    with db.session(cfg.database) as conn:
        for src, sku, title in titles:
            pid = db.upsert_product(
                conn, canonical_key=f"seed-{sku}", title=title, brand=None, series=None, realism=None,
                pack_size=1, is_set=False, image_url=None,
            )
            db.upsert_listing(conn, pid, {
                "source": src, "source_sku": sku, "region": "600127",
                "raw_title": title, "price": 599.0, "in_stock": True,
            })

    with db.session(cfg.database) as conn:
        before_listings = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
        before_history = conn.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]

    pipeline.regroup(cfg)

    with db.session(cfg.database) as conn:
        after_listings = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
        after_history = conn.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]
        products = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        orphans = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE product_id NOT IN (SELECT id FROM products)"
        ).fetchone()["c"]
        grouped = conn.execute(
            """SELECT COUNT(DISTINCT product_id) c FROM listings
                WHERE source_sku IN ('a1','f1','b1')"""
        ).fetchone()["c"]

    checks = [
        ("listings survive regroup", after_listings, before_listings),
        ("price history survives", after_history, before_history),
        ("no orphaned listings", orphans, 0),
        ("the three 5-pack titles share one product", grouped, 1),
        ("distinct products after regroup", products, 2),
    ]
    fails = 0
    for label, got, want in checks:
        ok = got == want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")

    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
