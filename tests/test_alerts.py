"""Alert transitions.

The failure mode that matters is not missing an alert - it is sending the same
one on every scheduled run, or dumping the whole catalogue after a cache loss.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import alerts, config, db  # noqa: E402

REGION = "600127"


def check(label, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


def seed_product(conn, title, brand="Hot Wheels", pack=1, realism="Realistic"):
    return db.upsert_product(
        conn, canonical_key=f"k-{title}", title=title, brand=brand,
        series=None, realism=realism, pack_size=pack, is_set=False, image_url=None,
    )


def put(conn, pid, source, sku, price, in_stock):
    db.upsert_listing(conn, pid, {
        "source": source, "source_sku": sku, "region": REGION,
        "raw_title": "x", "price": price, "in_stock": in_stock,
    })


def main() -> int:
    fails = 0
    cfg = config.load()
    cfg.database = Path(tempfile.mkdtemp()) / "alerts.db"

    with db.session(cfg.database) as conn:
        pid = seed_product(conn, "Hot Wheels Nissan Skyline GT-R")
        other = seed_product(conn, "Matchbox Ford Bronco", brand="Matchbox")
        put(conn, pid, "blinkit", "b1", 499.0, True)
        put(conn, other, "blinkit", "b2", 300.0, True)
        conn.execute(
            """INSERT INTO watchlist (query, target_price, brand, stock_only, created_at)
               VALUES (?,?,?,?,?)""",
            ("skyline", 400.0, "Hot Wheels", 1, db.now()),
        )
        conn.commit()

    # 1. Seeding must record silently.
    with db.session(cfg.database) as conn:
        first = alerts.evaluate(conn, region=REGION, seed=True)
        fails += not check("seed sends nothing", len(first), 0)
        n = conn.execute("SELECT COUNT(*) c FROM alert_state").fetchone()["c"]
        fails += not check("seed records state (brand filter applied)", n, 1)

    # 2. Nothing changed -> nothing sent.
    with db.session(cfg.database) as conn:
        fails += not check("no change is silent", len(alerts.evaluate(conn, region=REGION)), 0)

    # 3. Goes out of stock, then returns.
    with db.session(cfg.database) as conn:
        put(conn, pid, "blinkit", "b1", 499.0, False)
        fails += not check("going out of stock is not an alert",
                           len(alerts.evaluate(conn, region=REGION)), 0)
    with db.session(cfg.database) as conn:
        put(conn, pid, "blinkit", "b1", 499.0, True)
        got = alerts.evaluate(conn, region=REGION)
        fails += not check("restock alerts once", [a.kind for a in got], [alerts.BACK_IN_STOCK])
    with db.session(cfg.database) as conn:
        fails += not check("restock does not repeat",
                           len(alerts.evaluate(conn, region=REGION)), 0)

    # 4. Price falls below the target.
    with db.session(cfg.database) as conn:
        put(conn, pid, "blinkit", "b1", 350.0, True)
        got = alerts.evaluate(conn, region=REGION)
        fails += not check("price drop alerts", [a.kind for a in got], [alerts.PRICE_DROP])
        fails += not check("carries the old price", got[0].previous_price if got else None, 499.0)
    with db.session(cfg.database) as conn:
        put(conn, pid, "blinkit", "b1", 340.0, True)
        fails += not check("further drop below target is not re-sent",
                           len(alerts.evaluate(conn, region=REGION)), 0)

    # 5. A new shop for a matched product.
    with db.session(cfg.database) as conn:
        put(conn, pid, "funcorp", "f1", 380.0, True)
        got = alerts.evaluate(conn, region=REGION)
        fails += not check("new shop alerts as new", [a.kind for a in got], [alerts.NEW])

    # 6. Another region must not leak in.
    with db.session(cfg.database) as conn:
        db.upsert_listing(conn, pid, {
            "source": "blinkit", "source_sku": "b1", "region": "560001",
            "raw_title": "x", "price": 99.0, "in_stock": True})
        fails += not check("other region ignored",
                           len(alerts.evaluate(conn, region=REGION)), 0)

    # 8. Several listings from one shop for the same product must collapse to
    #    one state row, or the remembered state flips and invents restocks.
    with db.session(cfg.database) as conn:
        put(conn, pid, "blinkit", "b1", 340.0, True)
        put(conn, pid, "blinkit", "b1-dup", 999.0, False)
        conn.commit()
        watch = alerts.active_watches(conn)[0]
        rows = alerts.matches(conn, watch, REGION)
        pairs = [(r["product_id"], r["source"]) for r in rows]
        fails += not check("one row per (product, shop)", len(pairs), len(set(pairs)))
        best = [r for r in rows if r["source"] == "blinkit"][0]
        fails += not check("keeps the buyable offer", (best["in_stock"], best["price"]), (1, 340.0))
    with db.session(cfg.database) as conn:
        alerts.evaluate(conn, region=REGION)
    with db.session(cfg.database) as conn:
        fails += not check("duplicate listings do not cause repeat alerts",
                           len(alerts.evaluate(conn, region=REGION)), 0)

    # 7. Message rendering escapes HTML.
    a = alerts.Alert(kind=alerts.NEW, watch_id=1, watch_query="x", product_id=1,
                     title="Hot Wheels <Fast & Furious>", source="blinkit",
                     price=499.0, url="http://e.com", in_stock=True)
    msg = alerts.format_message([a])
    fails += not check("title is HTML-escaped", "&lt;Fast &amp; Furious&gt;" in msg, True)

    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
