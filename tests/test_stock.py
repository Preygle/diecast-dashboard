"""Out-of-stock handling.

A sold-out listing usually renders with no price at all. Those must be stored
(not dropped), must never become a product's "best price", and the nullable
price migration must not destroy existing rows or their history.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import config, db, queries  # noqa: E402


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


def main() -> int:
    fails = 0
    cfg = config.load()
    cfg.database = Path(tempfile.mkdtemp()) / "stock.db"

    # An old-schema database, to prove the migration preserves data.
    legacy = db.SCHEMA.replace("price         REAL,", "price         REAL NOT NULL,")
    import sqlite3
    raw = sqlite3.connect(str(cfg.database))
    raw.executescript(legacy)
    raw.close()

    with db.session(cfg.database) as conn:
        pid = db.upsert_product(
            conn, canonical_key="k|sig|p1", title="Hot Wheels Test Car",
            brand="Hot Wheels", series=None, realism=None, pack_size=1, is_set=False, image_url=None,
        )
        # in stock and priced
        db.upsert_listing(conn, pid, {
            "source": "amazon_in", "source_sku": "a1", "region": "600127",
            "raw_title": "Hot Wheels Test Car", "price": 500.0, "in_stock": True})
        # sold out, no price at all
        db.upsert_listing(conn, pid, {
            "source": "flipkart", "source_sku": "f1", "region": "600127",
            "raw_title": "Hot Wheels Test Car", "price": None, "in_stock": False})
        # sold out but the site still shows a (higher) price
        db.upsert_listing(conn, pid, {
            "source": "blinkit", "source_sku": "b1", "region": "600127",
            "raw_title": "Hot Wheels Test Car", "price": 900.0, "in_stock": False})

    with db.session(cfg.database) as conn:
        stored = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
        nulls = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE price IS NULL").fetchone()["c"]
        hist = conn.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]

        rows = queries.catalog(conn, sort="price")
        row = rows[0]

        in_only = queries.catalog(conn, in_stock_only=True)
        oos_only = queries.catalog(conn, oos_only=True)
        facets = queries.facets(conn)
        st = queries.stats(conn)

    checks = [
        ("unpriced sold-out listing is stored", stored, 3),
        ("its price is NULL, not 0", nulls, 1),
        ("no history row for an unpriced sighting", hist, 2),
        ("best price ignores the unpriced offer", row["best_price"], 500.0),
        ("all three offers are shown", len(row["offers"]), 3),
        ("out-of-stock offers counted", row["oos_count"], 2),
        ("buyable offers counted", row["buyable_count"], 1),
        ("in-stock filter still finds it", len(in_only), 1),
        ("out-of-stock filter finds it", len(oos_only), 1),
        ("stats cheapest ignores sold-out", st["cheapest"], 500.0),
        ("stats counts out-of-stock", st["out_of_stock"], 2),
        ("availability facet present", len(facets["availability"]), 2),
    ]
    for label, got, want in checks:
        fails += not check(label, got, want)

    # A ceiling must not hide an unpriced listing.
    with db.session(cfg.database) as conn:
        capped = queries.catalog(conn, max_price=600)
        offers = capped[0]["offers"] if capped else []
    fails += not check(
        "price cap keeps the unpriced offer",
        any(o["price"] is None for o in offers), True)

    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
