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
from collections import Counter

# The Windows console defaults to cp1252, which cannot encode the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hotwheels import config, db, pipeline, queries  # noqa: E402


def cmd_scrape(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    cap = f"cap ₹{cfg.max_price:.0f}" if cfg.max_price else "no price cap"
    taste = "mainlines only" if cfg.exclude_fantasy else "all castings"
    print(f"Region: {cfg.region.label} ({cfg.region.pincode})  |  {cap}  |  {taste}")
    # --fast narrows both axes: the watched brands, at the shops that stock
    # them near MRP. That is what makes a two-minute poll cycle possible.
    only = args.only or (cfg.fast_sources if args.fast else None)
    report = asyncio.run(pipeline.scrape(cfg, only=only, fast=args.fast))
    print(report.render())
    # Exit non-zero only when a source actually errored. Finding nothing is a
    # legitimate result and must not fail a scheduled run.
    return 0 if all(r.ok for r in report.results) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hotwheels.server import create_app

    app = create_app(args.config)
    print(f"Dashboard -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    """Evaluate watch rules and notify about anything that changed."""
    from hotwheels import alerts, bot, notify
    from hotwheels.sources import source_labels

    cfg = config.load(args.config)
    labels = source_labels(cfg)

    with db.session(cfg.database) as conn:
        # Rules declared in config are the source of truth. A CI runner starts
        # with an empty database and would otherwise have no rules at all,
        # which reads as "nothing matched" rather than "nothing to match with".
        synced = alerts.sync_watchlist(conn, cfg.raw.get("watchlist") or [])
        if synced:
            print(f"Watchlist: {synced} rule(s) synced from config.")

        # A database with rules but no remembered state has never run. Every
        # match would look new, which on a fresh CI cache means hundreds of
        # messages about a catalogue that was already there. Record it instead.
        first_run = not args.seed and conn.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM alert_state)"
        ).fetchone()[0]
        if first_run:
            print("No alert state yet - recording the catalogue instead of "
                  "announcing it. The next run reports changes.")

        found = alerts.evaluate(conn, region=cfg.region.pincode,
                                seed=args.seed or bool(first_run),
                                max_markup=cfg.max_markup, mrp=cfg.mrp)
        if first_run:
            return 0
        # Muting is applied after evaluation, never before: state has to keep
        # advancing while a shop is quiet, or unmuting would dump every change
        # that happened in the meantime.
        found, held = bot.suppress(conn, found)
        photos = bot.photos_on(conn)

    if args.seed:
        print("Seeded alert state; nothing sent.")
        return 0
    if held:
        print(f"Nothing sent - {held}.")
        return 0
    if not found:
        print("No changes since the last check.")
        return 0

    text = alerts.format_message(found, lambda s: labels.get(s, s))
    print(f"{len(found)} alert(s):")
    for a in found:
        money = f"Rs {a.price:,.0f}" if a.price is not None else "no price"
        print(f"  [{a.kind}] {a.title[:52]} - {money} at {labels.get(a.source, a.source)}")

    if args.dry_run:
        print("")
        print("--- dry run, not sent ---")
        return 0

    # Photos go through the Telegram client directly, because an album is not
    # something `broadcast` can express. Discord still gets the text digest.
    tg, dc = notify.build()
    shape = "text"
    if tg is not None and photos:
        sent, shape = bot.send_with_photos(tg, found, text, lambda s: labels.get(s, s))
        if dc is not None:
            try:
                dc.send(text)
                sent.append("discord")
            except notify.NotifyError as exc:
                print(f"  discord failed: {exc}")
    else:
        sent = notify.broadcast(text)

    # Record the attempt either way. alert_state says what was *seen*; this is
    # the only record of what was actually delivered, which is the question
    # asked whenever the bot seems quiet.
    kinds = ", ".join(f"{k}={v}" for k, v in
                      sorted(Counter(a.kind for a in found).items()))
    with db.session(cfg.database) as conn:
        if sent:
            for channel in sent:
                db.log_send(conn, channel=channel,
                            shape=shape if channel == "telegram" else "text",
                            alerts=len(found), kinds=kinds, ok=True)
        else:
            db.log_send(conn, channel="telegram", shape=shape, alerts=len(found),
                        kinds=kinds, ok=False, error="no channel accepted it")

    if not sent:
        print("Could not send. Run `python cli.py notify status`.")
        return 1
    print("Sent via:", ", ".join(sent))
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    """Answer commands people sent the bot, and keep its menu in step.

    Polling rather than a webhook is deliberate: this runs from the same
    scheduled task as the scraper, so commands are answered a few minutes
    after they are sent without anything listening in between.
    """
    from hotwheels import bot, notify
    from hotwheels.sources import source_labels

    cfg = config.load(args.config)
    labels = source_labels(cfg)

    with db.session(cfg.database) as conn:
        if args.action == "commands":
            for name in args.on:
                bot.set_enabled(conn, name.lstrip("/"), True)
            for name in args.off:
                bot.set_enabled(conn, name.lstrip("/"), False)
            for row in bot.command_table(conn):
                mark = "on " if row["enabled"] else "off"
                lock = " (always on)" if row["always_on"] else ""
                print(f"  {mark}  {row['slash']:<10} {row['summary']}{lock}")
            if not (args.on or args.off):
                return 0

        tg, _ = notify.build()
        if tg is None:
            print("No TELEGRAM_BOT_TOKEN set. Run `python cli.py notify status`.")
            return 1

        # Telegram caches the / menu, so push it whenever the set may have
        # changed - otherwise a freshly enabled command stays invisible.
        try:
            tg.call("setMyCommands", commands=bot.menu(conn))
        except Exception as exc:
            print(f"  could not update the command menu: {exc}")

        if args.action in ("commands", "menu"):
            print(f"Menu now advertises {len(bot.menu(conn))} command(s).")
            return 0

        handled = bot.poll(conn, cfg, tg, labels)

    if not handled:
        print("No new commands.")
        return 0
    for text, outcome in handled:
        print(f"  {text} -> {outcome}")
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Configure and test the alert channels."""
    from hotwheels import notify

    env = notify.load_env()
    tg, dc = notify.build(env)

    if args.action == "chatid":
        if tg is None:
            print("No TELEGRAM_BOT_TOKEN in .env")
            return 1
        chat_id = tg.discover_chat_id()
        if not chat_id:
            print("The bot has not received any message yet.")
            print(f"  Open https://t.me/{tg.me().get('username')} and send it /start,")
            print("  then run this again.")
            return 1
        notify.set_env_value("TELEGRAM_CHAT_ID", chat_id)
        print(f"Saved TELEGRAM_CHAT_ID={chat_id} to .env")
        return 0

    if args.action == "status":
        print(f"Telegram: {'configured' if tg else 'missing token'}", end="")
        if tg:
            try:
                print(f" (@{tg.me().get('username')}, chat_id={tg.chat_id or 'NOT SET'})")
            except notify.NotifyError as exc:
                print(f" - {exc}")
        else:
            print()
        print(f"Discord : {'configured' if dc else 'not set'}")
        return 0

    # test
    text = args.message or (
        "<b>Diecast Dashboard</b> is connected." "\n\n"
        "You will get a message here when a watched model comes back in "
        "stock or drops below your target price."
    )
    sent = notify.broadcast(text, env)
    if not sent:
        print("Nothing sent. Run `python cli.py notify status` to see why.")
        return 1
    print("Sent via:", ", ".join(sent))
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
                """INSERT INTO watchlist
                       (query, note, target_price, brand, realism, series,
                        pack_min, pack_max, sources, stock_only, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (args.query, args.note, args.target, args.brand, args.realism,
                 args.series, args.pack_min, args.pack_max, args.sources,
                 0 if args.include_oos else 1, db.now()),
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
    s.add_argument("--fast", action="store_true",
                   help="use fast_queries - the watched brands only, for quick polling")
    s.set_defaults(fn=cmd_scrape)

    s = sub.add_parser("serve", help="run the dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("alerts", help="check watch rules and send notifications")
    s.add_argument("--seed", action="store_true",
                   help="record current state without sending (first run)")
    s.add_argument("--dry-run", action="store_true", help="print instead of sending")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("bot", help="answer Telegram commands sent since last run")
    s.add_argument("action", nargs="?", default="poll",
                   choices=["poll", "commands", "menu"])
    s.add_argument("--on", nargs="*", default=[], help="enable these commands")
    s.add_argument("--off", nargs="*", default=[], help="disable these commands")
    s.set_defaults(fn=cmd_bot)

    s = sub.add_parser("notify", help="configure and test alert channels")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "chatid", "test"])
    s.add_argument("--message", default=None, help="custom text for `test`")
    s.set_defaults(fn=cmd_notify)

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
    s.add_argument("--brand", default=None, help="e.g. Hot Wheels, Matchbox, Majorette")
    s.add_argument("--realism", default=None, choices=["Realistic", "Fantasy"])
    s.add_argument("--series", default=None, help="e.g. Mainline, Premium, Track Set")
    s.add_argument("--pack-min", type=int, default=None, dest="pack_min")
    s.add_argument("--pack-max", type=int, default=None, dest="pack_max")
    s.add_argument("--sources", default=None, help="comma-separated shop ids")
    s.add_argument("--include-oos", action="store_true", dest="include_oos",
                   help="also alert on listings that are not buyable")
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
