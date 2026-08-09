#!/usr/bin/env python
"""Hot Wheels dashboard CLI.

  python cli.py scrape                 # all enabled sources
  python cli.py scrape --only amazon_in flipkart
  python cli.py serve                  # dashboard at http://127.0.0.1:8000
  python cli.py watch add "treasure hunt" --target 500
  python cli.py watch list
  python cli.py top --limit 15
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# The Windows console defaults to cp1252, which cannot encode the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hotwheels import config, db, pipeline, queries  # noqa: E402


def cmd_scrape(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    print(f"Region: {cfg.region.label} ({cfg.region.pincode})  |  cap ₹{cfg.max_price:.0f}")
    report = asyncio.run(pipeline.scrape(cfg, only=args.only))
    print(report.render())
    return 0 if any(r.ok and r.items for r in report.results) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hotwheels.server import create_app

    app = create_app(args.config)
    print(f"Dashboard -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_region(args: argparse.Namespace) -> int:
    """Show, list, or change the delivery region."""
    from hotwheels import regions

    cfg = config.load(args.config)

    if args.action == "list":
        print(f"{'City':<22}{'Pincode':<10}State")
        print("-" * 56)
        for r in regions.known_regions():
            print(f"  {r['label']:<20}{r['pincode']:<10}{r['state']}")
        print("\nAny other Indian pincode is looked up online:")
        print("  python cli.py region set 560103")
        return 0

    if args.action == "show":
        print(f"Current region: {regions.describe(cfg.region)}")
        with db.session(cfg.database) as conn:
            rows = conn.execute(
                "SELECT region, COUNT(*) n FROM listings GROUP BY region ORDER BY n DESC"
            ).fetchall()
        if rows:
            print("\nListings stored per region:")
            for r in rows:
                mark = "  <- active" if r["region"] == cfg.region.pincode else ""
                print(f"   {r['region']:<10}{r['n']:>7}{mark}")
        return 0

    # set
    if args.lat is not None and args.lon is not None:
        if not args.pincode:
            print("--pincode is required with --lat/--lon")
            return 1
        region = config.Region(
            label=args.label or f"Custom {args.pincode}", pincode=args.pincode,
            city=args.label or args.pincode, lat=args.lat, lon=args.lon,
        )
    else:
        if not args.query:
            print("Give a city or pincode: python cli.py region set Chennai")
            return 1
        try:
            region = regions.resolve(args.query, online=not args.offline)
        except ValueError as exc:
            print(exc)
            return 1

    path = regions.save(region, args.config)
    print(f"Region set to {regions.describe(region)}")
    print(f"Saved to {path}")
    print("\nNext:")
    print("  python cli.py setup blinkit    # re-set the address in each quick-commerce app")
    print("  python cli.py scrape           # fetch prices for the new region")
    return 0


def cmd_regroup(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    before, after = pipeline.regroup(cfg)
    print(f"Rebuilt products from stored listings: {before} -> {after}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Open a quick-commerce site in a visible browser to set the address once.

    Blinkit accepts coordinates from cookies, but Zepto and Instamart bind the
    catalog to an address chosen in their UI (and Swiggy puts a WAF challenge in
    front of fresh sessions). Doing it by hand once writes both into the saved
    profile, which every later scrape reuses.
    """
    from hotwheels.sources import REGISTRY

    cfg = config.load(args.config)
    cls = REGISTRY.get(args.source)
    if cls is None or not hasattr(cls, "setup"):
        print(f"'{args.source}' has no setup step. Options: blinkit, instamart, zepto")
        return 1

    src = cls(cfg)
    print(f"Opening {src.label}. Set delivery to {cfg.region.label} ({cfg.region.pincode}).")
    asyncio.run(src.setup())
    print("Saved to the browser profile. Re-run `python cli.py scrape`.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    with db.session(cfg.database) as conn:
        if args.action == "add":
            conn.execute(
                "INSERT INTO watchlist (query, note, target_price, created_at) VALUES (?,?,?,?)",
                (args.query, args.note, args.target, db.now()),
            )
            print(f"Watching: {args.query!r}" + (f" under ₹{args.target:.0f}" if args.target else ""))
        elif args.action == "remove":
            conn.execute("UPDATE watchlist SET active = 0 WHERE id = ?", (args.id,))
            print(f"Removed watch #{args.id}")
        else:
            rows = queries.watchlist(conn)
            if not rows:
                print("Watchlist is empty.")
                return 0
            for w in rows:
                flag = "  <-- TARGET MET" if w["hit"] else ""
                best = f"₹{w['best_price']:.0f}" if w["best_price"] else "no match"
                target = f" (target ₹{w['target_price']:.0f})" if w["target_price"] else ""
                print(f"  #{w['id']:<3} {w['query']:<32} best {best}{target}{flag}")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    with db.session(cfg.database) as conn:
        rows = queries.catalog(conn, sort="price", limit=args.limit)
        if not rows:
            print("No data yet - run `python cli.py scrape` first.")
            return 1
        print(f"\n{'Price':>8}  {'Sites':>5}  Title")
        print("-" * 78)
        for r in rows:
            sites = ",".join(sorted({o["source"] for o in r["offers"]}))
            print(f"₹{r['best_price']:>7.0f}  {r['source_count']:>5}  {r['title'][:52]}")
            print(f"{'':>16}{sites}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    with db.session(cfg.database) as conn:
        s = queries.stats(conn)
        print(f"\nProducts: {s['products']}   Listings: {s['listings']}   Sources: {s['sources']}")
        print(f"Last scraped: {s['last_scraped'] or 'never'}\n")
        print(f"  {'Source':<14}{'Listings':>9}{'Cheapest':>10}{'Average':>10}")
        print("  " + "-" * 43)
        for r in s["per_source"]:
            cheap = f"₹{r['cheapest']:.0f}" if r["cheapest"] else "-"
            avg = f"₹{r['average']:.0f}" if r["average"] else "-"
            print(f"  {r['source']:<14}{r['listings']:>9}{cheap:>10}{avg:>10}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="hotwheels", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="fetch listings from every enabled source")
    s.add_argument("--only", nargs="*", help="limit to these source names")
    s.set_defaults(fn=cmd_scrape)

    s = sub.add_parser("serve", help="run the dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("region", help="show or change the delivery region")
    s.add_argument("action", nargs="?", default="show", choices=["show", "list", "set"])
    s.add_argument("query", nargs="?", default=None, help="city name or 6-digit pincode")
    s.add_argument("--lat", type=float, default=None)
    s.add_argument("--lon", type=float, default=None)
    s.add_argument("--pincode", default=None)
    s.add_argument("--label", default=None)
    s.add_argument("--offline", action="store_true", help="skip the online pincode lookup")
    s.set_defaults(fn=cmd_region)

    s = sub.add_parser("regroup", help="rebuild product grouping from stored listings")
    s.set_defaults(fn=cmd_regroup)

    s = sub.add_parser("setup", help="set a quick-commerce delivery address by hand (one time)")
    s.add_argument("source", choices=["blinkit", "instamart", "zepto"])
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("watch", help="manage the watchlist")
    s.add_argument("action", choices=["add", "list", "remove"])
    s.add_argument("query", nargs="?", default=None)
    s.add_argument("--target", type=float, default=None, help="alert below this price")
    s.add_argument("--note", default=None)
    s.add_argument("--id", type=int, default=None)
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("top", help="cheapest items found")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_top)

    s = sub.add_parser("stats", help="database summary")
    s.set_defaults(fn=cmd_stats)

    args = p.parse_args()

    if args.cmd == "watch":
        if args.action == "add" and not args.query:
            p.error("watch add needs a query")
        if args.action == "remove" and not args.id:
            p.error("watch remove needs --id")

    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
