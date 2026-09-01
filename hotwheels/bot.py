"""Telegram command handling.

The alerting side of the bot pushes; this is the side that answers. Commands
arrive through `getUpdates` rather than a webhook, because the whole point of
this project is that it survives on a laptop and a scheduled task - there is no
always-on listener to receive a webhook. `poll()` drains whatever was said
since last time, using a cursor in `bot_state` so a command is acted on exactly
once even though the poller only wakes every few minutes.

Every command is a row in `REGISTRY`. Some ship enabled, the rest ship off and
are turned on from the dashboard's Bot tab (or `/commands` in chat). A disabled
command is not merely refused - it is left out of the menu Telegram shows and
out of `/help`, so the bot never advertises something it will not do.

Two settings here also govern the *push* side, because muting belongs with the
thing you mute:

  filter.sources    shops whose alerts are suppressed
  alerts.paused     a timestamp until which nothing is sent at all

Both suppress delivery only. Alert state still advances underneath, so
unmuting a shop gives you its next change rather than a backlog of everything
that happened while it was quiet.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from . import db, queries

# bot_state keys
CMD_PREFIX = "cmd."
FILTER_SOURCES = "filter.sources"
PAUSED_UNTIL = "alerts.paused_until"
OFFSET = "bot.offset"
PHOTOS = "alerts.photos"

MAX_REPLY = 3800  # Telegram's hard cap is 4096; leave room for the footer.


@dataclass
class Command:
    name: str
    summary: str
    handler: Callable[..., str]
    usage: str = ""
    default_on: bool = False
    # /help must always answer, or a bad toggle leaves the bot unusable.
    always_on: bool = False

    @property
    def slash(self) -> str:
        return "/" + self.name


def _esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _rupee(v: Any) -> str:
    return f"Rs {v:,.0f}" if isinstance(v, (int, float)) else "no price"


# ---------------------------------------------------------------- settings

def enabled(conn: Any, name: str) -> bool:
    cmd = REGISTRY.get(name)
    if cmd is None:
        return False
    if cmd.always_on:
        return True
    raw = db.get_state(conn, CMD_PREFIX + name)
    return cmd.default_on if raw is None else raw == "1"


def set_enabled(conn: Any, name: str, on: bool) -> None:
    db.set_state(conn, CMD_PREFIX + name, "1" if on else "0")


def command_table(conn: Any) -> list[dict[str, Any]]:
    """Every command with its current state, for the dashboard."""
    return [
        {
            "name": c.name,
            "slash": c.slash,
            "summary": c.summary,
            "usage": c.usage,
            "default_on": c.default_on,
            "always_on": c.always_on,
            "enabled": enabled(conn, c.name),
        }
        for c in REGISTRY.values()
    ]


def muted_sources(conn: Any) -> set[str]:
    raw = db.get_state(conn, FILTER_SOURCES, "") or ""
    return {s for s in (p.strip() for p in raw.split(",")) if s}


def set_muted_sources(conn: Any, names: list[str]) -> None:
    db.set_state(conn, FILTER_SOURCES, ",".join(sorted(set(names))))


def photos_on(conn: Any) -> bool:
    """Whether alerts carry product photos. On unless turned off."""
    return (db.get_state(conn, PHOTOS) or "1") == "1"


def set_photos(conn: Any, on: bool) -> None:
    db.set_state(conn, PHOTOS, "1" if on else "0")


def paused_until(conn: Any) -> dt.datetime | None:
    raw = db.get_state(conn, PAUSED_UNTIL)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def is_paused(conn: Any) -> bool:
    until = paused_until(conn)
    return bool(until and until > dt.datetime.now(dt.timezone.utc))


def set_paused(conn: Any, hours: float | None) -> None:
    if not hours:
        db.set_state(conn, PAUSED_UNTIL, "")
        return
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    db.set_state(conn, PAUSED_UNTIL, until.isoformat())


def suppress(conn: Any, alerts: list[Any]) -> tuple[list[Any], str | None]:
    """Apply the mute settings to a batch of alerts about to be sent.

    Returns what survives, plus a reason when everything was held back - the
    caller prints it, so a silent run is explained rather than mysterious.
    """
    if is_paused(conn):
        until = paused_until(conn)
        return [], f"alerts paused until {until:%Y-%m-%d %H:%M} UTC"
    muted = muted_sources(conn)
    if not muted:
        return alerts, None
    kept = [a for a in alerts if a.source not in muted]
    if alerts and not kept:
        return [], f"all {len(alerts)} alert(s) came from muted shops"
    return kept, None


# Telegram's album limit. More alerts than this and the pictures become a
# supplement to the text digest rather than a replacement for it.
ALBUM_MAX = 10


def photo_caption(alert: Any, label: Any = None) -> str:
    """One picture's caption: what happened, what it is, what it costs."""
    from . import alerts as _alerts

    shop = label(alert.source) if callable(label) else alert.source
    money = f"Rs {alert.price:,.0f}" if alert.price is not None else "no price"
    bits = [f"<b>{_esc(alert.title)}</b>",
            f"{money} at {_esc(str(shop))} · {_alerts.HEADINGS.get(alert.kind, alert.kind)}"]
    if alert.kind == _alerts.PRICE_DROP and alert.previous_price:
        bits[1] += f" (was Rs {alert.previous_price:,.0f})"
    if alert.url:
        bits.append(_esc(alert.url))
    return "\n".join(bits)[:1024]


def send_with_photos(tg: Any, alerts_list: list[Any], text: str,
                     label: Any = None) -> list[str]:
    """Deliver a batch as pictures where possible, text where not.

    Titles like "HW Torque Free Wheel" identify nothing; the photo is what
    tells you which casting this is. The rules, in order:

      1 with a photo    one captioned picture
      2-10 with photos  one album, each captioned
      more than 10      the full text digest, plus an album of the first 10

    Any failure falls back to the text digest, because a picture is a nicety
    and a missed restock is not.
    """
    from . import notify

    withpics = [a for a in alerts_list if getattr(a, "image_url", None)]
    sent: list[str] = []

    try:
        if not withpics:
            tg.send(text)
            return ["telegram"]

        # A batch that fits in one album needs no text message: every caption
        # carries the price, the shop and the link already.
        covers_everything = len(withpics) == len(alerts_list) and len(withpics) <= ALBUM_MAX
        if not covers_everything:
            tg.send(text)
            sent.append("telegram")

        batch = withpics[:ALBUM_MAX]
        items = [{"url": notify.thumb_url(a.image_url),
                  "caption": photo_caption(a, label)} for a in batch]

        if len(items) == 1:
            tg.send_photo(items[0]["url"], items[0]["caption"])
        else:
            tg.send_album(items)
        if not sent:
            sent.append("telegram")
        return sent
    except notify.NotifyError as exc:
        print(f"  photos failed ({exc}); falling back to text")
        if sent:
            return sent
        tg.send(text)
        return ["telegram"]


# ---------------------------------------------------------------- handlers
# Each takes (conn, cfg, args, labels) and returns HTML for one reply.

def _labels(labels: dict[str, str], name: str) -> str:
    return labels.get(name, name)


def cmd_help(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    live = [c for c in REGISTRY.values() if enabled(conn, c.name)]
    off = [c for c in REGISTRY.values() if not enabled(conn, c.name)]
    lines = ["<b>Diecast watchdog</b>", ""]
    for c in live:
        lines.append(f"{c.slash} {_esc(c.usage)}".rstrip() + f" — {_esc(c.summary)}")
    if off:
        lines += ["", f"<i>{len(off)} more command(s) are switched off: "
                      + ", ".join(c.slash for c in off) + "</i>",
                  "<i>Turn them on in the dashboard's Bot tab.</i>"]
    return "\n".join(lines)


def cmd_status(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    st = queries.stats(conn, cfg.region.pincode)
    muted = muted_sources(conn)
    lines = [
        "<b>Status</b>",
        f"{st['products']:,} products · {st['listings']:,} listings · "
        f"{st['sources']} shops",
        f"Region {_esc(cfg.region.label)} · "
        + (f"cap {_rupee(cfg.max_price)}" if cfg.max_price else "no price cap")
        + (" · mainlines only" if cfg.exclude_fantasy else ""),
        f"Last scrape {_esc((st['last_scraped'] or '—')[:16].replace('T', ' '))}",
        "",
    ]
    for row in st["per_source"]:
        flag = " (muted)" if row["source"] in muted else ""
        lines.append(
            f"· {_esc(_labels(labels, row['source']))}: {row['listings']} listings, "
            f"cheapest {_rupee(row['cheapest'])}{flag}"
        )
    lines += ["", "Photos " + ("on" if photos_on(conn) else "off")]
    if is_paused(conn):
        lines += [f"<b>Alerts paused</b> until {paused_until(conn):%H:%M UTC}"]
    return "\n".join(lines)


def cmd_find(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    if not args:
        return "Give me something to look for, e.g. <code>/find supra</code>"
    rows = queries.catalog(conn, q=args, region=cfg.region.pincode,
                           sort="price", limit=8)
    if not rows:
        return f"Nothing matching <b>{_esc(args)}</b>."
    lines = [f"<b>{len(rows)} result(s) for {_esc(args)}</b>", ""]
    for p in rows:
        best = (p.get("offers") or [{}])[0]
        lines.append(f"<b>{_esc(p['title'][:70])}</b>")
        lines.append(f"{_rupee(p.get('best_price'))} at "
                     f"{_esc(_labels(labels, best.get('source', '?')))}")
        if best.get("url"):
            lines.append(_esc(best["url"]))
        lines.append("")
    return "\n".join(lines)


def cmd_deals(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    rows = queries.deals(conn, limit=8)
    if not rows:
        return "No price gaps worth reporting."
    lines = ["<b>Biggest price gaps</b>", ""]
    for d in rows:
        gap = (d.get("worst_price") or 0) - (d.get("best_price") or 0)
        lines.append(f"<b>{_esc(d['title'][:70])}</b>")
        lines.append(f"{_rupee(d.get('best_price'))} — {_rupee(d.get('worst_price'))}"
                     f" across {d.get('source_count', 0)} shops "
                     f"(save {_rupee(gap)})")
        lines.append("")
    return "\n".join(lines)


def cmd_watch(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    rows = queries.watchlist(conn)
    if not rows:
        return "No watch rules. Add one in the dashboard's Watchlist tab."
    lines = ["<b>Watch rules</b>", ""]
    for w in rows:
        bits = [f"#{w['id']} {_esc(w.get('note') or w.get('query'))}"]
        if w.get("target_price"):
            bits.append(f"target {_rupee(w['target_price'])}")
        if w.get("brand"):
            bits.append(_esc(w["brand"]))
        if w.get("realism"):
            bits.append(_esc(w["realism"]))
        lines.append("· " + " · ".join(bits))
        lines.append(f"   {len(w.get('matches') or [])} currently matching")
    return "\n".join(lines)


def cmd_stock(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    st = queries.stats(conn, cfg.region.pincode)
    lines = ["<b>Stock</b>", f"{st['out_of_stock']:,} listings out of stock", ""]
    for row in st["per_source"]:
        lines.append(f"· {_esc(_labels(labels, row['source']))}: "
                     f"{row['out_of_stock']} of {row['listings']} out")
    return "\n".join(lines)


def cmd_filter(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    """Mute or unmute shops. `/filter` alone shows the current picture."""
    known = list(labels.keys())
    muted = muted_sources(conn)
    arg = args.strip().lower()

    if arg:
        verb, _, rest = arg.partition(" ")
        names = [n.strip() for n in rest.replace(",", " ").split() if n.strip()]
        if verb in ("on", "unmute", "keep") and names:
            muted -= set(names)
            set_muted_sources(conn, list(muted))
        elif verb in ("off", "mute", "drop") and names:
            unknown = [n for n in names if n not in known]
            if unknown:
                return ("Unknown shop(s): " + _esc(", ".join(unknown))
                        + "\nKnown: " + _esc(", ".join(known)))
            muted |= set(names)
            set_muted_sources(conn, list(muted))
        elif verb in ("all", "reset", "clear"):
            set_muted_sources(conn, [])
            muted = set()
        else:
            return ("Usage:\n<code>/filter off blinkit crossword</code>\n"
                    "<code>/filter on blinkit</code>\n<code>/filter reset</code>")

    lines = ["<b>Alert filter</b>", ""]
    for name in known:
        on = name not in muted
        lines.append(f"{'✅' if on else '🔕'} {_esc(labels[name])} "
                     f"<code>{_esc(name)}</code>")
    lines += ["", "<i>Muted shops are still scraped and still shown in the "
                  "dashboard — you just stop being messaged about them.</i>"]
    return "\n".join(lines)


def cmd_photos(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    """Product photos in alerts. Titles alone rarely identify a casting."""
    arg = args.strip().lower()
    if arg in ("on", "off"):
        set_photos(conn, arg == "on")
    elif arg:
        return "Usage: <code>/photos on</code> or <code>/photos off</code>"

    if photos_on(conn):
        return ("📷 Photos are <b>on</b>. Alerts arrive as pictures with the "
                "price in the caption; a digest of more than 10 also gets the "
                "full text list.\n<code>/photos off</code> for text only.")
    return ("Photos are <b>off</b> - alerts are text only.\n"
            "<code>/photos on</code> to see the casting.")


def cmd_pause(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    try:
        hours = float(args.strip()) if args.strip() else 8.0
    except ValueError:
        return "Give me a number of hours, e.g. <code>/pause 12</code>"
    hours = max(0.1, min(hours, 24 * 14))
    set_paused(conn, hours)
    return (f"Alerts paused for {hours:g}h, until "
            f"{paused_until(conn):%Y-%m-%d %H:%M} UTC.\n"
            "Scraping continues; you just will not be messaged. "
            "<code>/resume</code> ends it early.")


def cmd_resume(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    set_paused(conn, None)
    return "Alerts resumed. You will hear about the next change, not the backlog."


def cmd_region(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    r = cfg.region
    return ("<b>Delivery region</b>\n"
            f"{_esc(r.label)} · {_esc(r.pincode)} · {_esc(r.city)}\n"
            f"lat {r.lat}, lon {r.lon}\n\n"
            "<i>Change it in the dashboard header — quick-commerce prices "
            "are quoted per delivery area, so switching re-scopes them.</i>")


def cmd_stats(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    st = queries.stats(conn, cfg.region.pincode)
    lines = [
        "<b>Database</b>",
        f"products     {st['products']:,}",
        f"listings     {st['listings']:,}",
        f"shops        {st['sources']}",
        f"out of stock {st['out_of_stock']:,}",
        f"cheapest     {_rupee(st['cheapest'])}",
        "",
        "<b>Regions held</b>",
    ]
    for r in st["regions"]:
        lines.append(f"· {_esc(r['region'])}: {r['n']:,}")
    return "\n".join(lines)


def cmd_top(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    try:
        n = min(max(int(args.strip()), 1), 15) if args.strip() else 8
    except ValueError:
        n = 8
    rows = queries.catalog(conn, region=cfg.region.pincode, sort="price",
                           in_stock_only=True, limit=n)
    if not rows:
        return "Nothing in stock right now."
    lines = [f"<b>{n} cheapest in stock</b>", ""]
    for p in rows:
        best = (p.get("offers") or [{}])[0]
        lines.append(f"{_rupee(p.get('best_price'))} · {_esc(p['title'][:56])} "
                     f"({_esc(_labels(labels, best.get('source', '?')))})")
    return "\n".join(lines)


def cmd_history(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    if not args.strip().isdigit():
        return "Give me a product id, e.g. <code>/history 1452</code>"
    rows = queries.history(conn, int(args.strip()))
    if not rows:
        return f"No price history for product {_esc(args.strip())}."
    lines = [f"<b>Price history — product {_esc(args.strip())}</b>", ""]
    for h in rows[-12:]:
        stock = "" if h.get("in_stock") else " (out of stock)"
        lines.append(f"{_esc(str(h['ts'])[:16].replace('T', ' '))}  "
                     f"{_rupee(h.get('price'))}{stock}")
    return "\n".join(lines)


def cmd_commands(conn: Any, cfg: Any, args: str, labels: dict[str, str]) -> str:
    """Toggle commands from chat, mirroring the dashboard's Bot tab."""
    arg = args.strip().lower()
    if arg:
        verb, _, rest = arg.partition(" ")
        names = [n.strip().lstrip("/") for n in rest.replace(",", " ").split()]
        names = [n for n in names if n]
        unknown = [n for n in names if n not in REGISTRY]
        if unknown:
            return "Unknown command(s): " + _esc(", ".join(unknown))
        if verb == "on" and names:
            for n in names:
                set_enabled(conn, n, True)
        elif verb == "off" and names:
            locked = [n for n in names if REGISTRY[n].always_on]
            if locked:
                return _esc(", ".join("/" + n for n in locked)) + " cannot be turned off."
            for n in names:
                set_enabled(conn, n, False)
        else:
            return ("Usage:\n<code>/commands on pause resume</code>\n"
                    "<code>/commands off top</code>")

    lines = ["<b>Commands</b>", ""]
    for c in REGISTRY.values():
        on = enabled(conn, c.name)
        lock = " 🔒" if c.always_on else ""
        lines.append(f"{'✅' if on else '⬜'} {c.slash}{lock} — {_esc(c.summary)}")
    lines += ["", "<i>Toggle here or in the dashboard's Bot tab.</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- registry
# Seven ship on: enough to answer "what is going on" and "stop telling me
# about X" without touching the dashboard. The rest are off by default -
# they are either niche, or they change things, and an unused command
# cluttering the menu is worse than one command too few.

_ALL = [
    Command("help", "what this bot can do", cmd_help, always_on=True),
    Command("status", "pipeline health and per-shop counts", cmd_status,
            default_on=True),
    Command("filter", "mute or unmute shops", cmd_filter,
            usage="[off|on|reset] [shop…]", default_on=True),
    Command("find", "search the catalogue", cmd_find, usage="<text>",
            default_on=True),
    Command("deals", "biggest price gaps between shops", cmd_deals,
            default_on=True),
    Command("watch", "your watch rules and what they match", cmd_watch,
            default_on=True),
    Command("stock", "what is out of stock, per shop", cmd_stock,
            default_on=True),
    Command("photos", "product photos in alerts, on or off", cmd_photos,
            usage="[on|off]", default_on=True),
    # Off by default.
    Command("pause", "stop alerts for a while", cmd_pause, usage="[hours]"),
    Command("resume", "start alerts again", cmd_resume),
    Command("top", "cheapest items in stock", cmd_top, usage="[count]"),
    Command("stats", "full database summary", cmd_stats),
    Command("region", "which delivery area prices are scoped to", cmd_region),
    Command("history", "price history for one product", cmd_history,
            usage="<product id>"),
    Command("commands", "turn commands on and off", cmd_commands,
            usage="[on|off] [name…]"),
]

REGISTRY: dict[str, Command] = {c.name: c for c in _ALL}


# ---------------------------------------------------------------- dispatch

def dispatch(conn: Any, cfg: Any, text: str, labels: dict[str, str]) -> str | None:
    """Turn one message into a reply, or None if it is not for us."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None

    head, _, args = text.partition(" ")
    # Telegram appends @botname when several bots share a group.
    name = head[1:].split("@", 1)[0].lower()

    cmd = REGISTRY.get(name)
    if cmd is None:
        known = [c.slash for c in REGISTRY.values() if enabled(conn, c.name)]
        return (f"Unknown command {_esc(head)}.\nTry: " + ", ".join(known))
    if not enabled(conn, name):
        return (f"{cmd.slash} is switched off.\nTurn it on in the dashboard's "
                f"Bot tab, or with <code>/commands on {name}</code> "
                f"(if that command is enabled).")

    try:
        return cmd.handler(conn, cfg, args.strip(), labels)[:MAX_REPLY]
    except Exception as exc:  # a broken command must not kill the poller
        return f"{cmd.slash} failed: {_esc(type(exc).__name__)}: {_esc(exc)}"


def menu(conn: Any) -> list[dict[str, str]]:
    """The command list Telegram shows in its / menu, enabled ones only."""
    return [{"command": c.name, "description": c.summary[:256]}
            for c in REGISTRY.values() if enabled(conn, c.name)]


def poll(conn: Any, cfg: Any, tg: Any, labels: dict[str, str],
         *, limit: int = 40) -> list[tuple[str, str]]:
    """Drain pending updates, answer each command, advance the cursor.

    The cursor is only advanced past a message once its reply has been sent,
    so a crash mid-batch repeats that message rather than swallowing it.
    """
    raw = db.get_state(conn, OFFSET)
    offset = int(raw) + 1 if raw and raw.lstrip("-").isdigit() else None

    handled: list[tuple[str, str]] = []
    for update in tg.updates(offset=offset)[:limit]:
        uid = update.get("update_id")
        msg = update.get("message") or update.get("channel_post") or {}
        text = msg.get("text") or ""
        reply = dispatch(conn, cfg, text, labels)
        if reply:
            chat = str((msg.get("chat") or {}).get("id") or tg.chat_id or "")
            try:
                tg.call("sendMessage", chat_id=chat, text=reply[:4096],
                        parse_mode="HTML", disable_web_page_preview=True)
                handled.append((text.split(" ")[0], "ok"))
            except Exception as exc:
                handled.append((text.split(" ")[0], f"send failed: {exc}"))
        if uid is not None:
            db.set_state(conn, OFFSET, str(uid))
    return handled
