"""Turn watch rules into notifications.

The hard part is not finding matches - it is only reporting *changes*. A
scheduled run fires every few hours against a catalogue of thousands, so
without state every run would re-send everything it matched. `alert_state`
remembers what each rule was last told, and only transitions are sent:

  new          a matching product appeared that this rule had never seen
  back_in_stock  a known listing went out-of-stock -> in stock
  price_drop   a known listing fell to or below the rule's target price

Seeding matters as much as detecting. On a first run - or after the cached
database is lost on CI - everything looks new, which would mean hundreds of
messages. `evaluate(seed=True)` records the world as it is and sends nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db

NEW = "new"
BACK_IN_STOCK = "back_in_stock"
PRICE_DROP = "price_drop"

HEADINGS = {
    NEW: "New listing",
    BACK_IN_STOCK: "Back in stock",
    PRICE_DROP: "Price drop",
}


@dataclass
class Alert:
    kind: str
    watch_id: int
    watch_query: str
    product_id: int
    title: str
    source: str
    price: float | None
    url: str | None
    in_stock: bool
    target_price: float | None = None
    previous_price: float | None = None
    image_url: str | None = None
    # How far over its class median this price sits; None if unjudgeable.
    markup: float | None = None

    def line(self, label: Any = None) -> str:
        shop = label(self.source) if callable(label) else self.source
        money = f"Rs {self.price:,.0f}" if self.price is not None else "no price"
        bits = [f"<b>{_esc(self.title)}</b>", f"{money} at {_esc(str(shop))}"]
        if self.kind == PRICE_DROP and self.previous_price:
            bits[1] += f" (was Rs {self.previous_price:,.0f})"
        if self.target_price and self.price is not None and self.kind == PRICE_DROP:
            bits.append(f"target Rs {self.target_price:,.0f}")
        if self.url:
            bits.append(self.url)
        return "\n".join(bits)


def _esc(text: str) -> str:
    """Telegram HTML needs these three escaped; product titles contain them."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def active_watches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM watchlist WHERE active = 1 ORDER BY id"
    ).fetchall()]


def matches(conn: sqlite3.Connection, watch: dict[str, Any],
            region: str | None = None) -> list[dict[str, Any]]:
    """Every listing a watch rule currently covers."""
    where = ["1=1"]
    params: list[Any] = []

    if region:
        where.append("l.region = ?")
        params.append(region)

    # A query of "*" (or blank) means "anything matching the other filters",
    # which is how a pure brand/pack rule is expressed.
    q = (watch.get("query") or "").strip()
    if q and q != "*":
        where.append("p.title LIKE ?")
        params.append(f"%{q}%")

    for field, column in (("brand", "p.brand"), ("realism", "p.realism"),
                          ("series", "p.series")):
        if watch.get(field):
            where.append(f"{column} = ?")
            params.append(watch[field])

    if watch.get("pack_min") is not None:
        where.append("p.pack_size >= ?")
        params.append(watch["pack_min"])
    if watch.get("pack_max") is not None:
        where.append("p.pack_size <= ?")
        params.append(watch["pack_max"])

    if watch.get("sources"):
        names = [s.strip() for s in str(watch["sources"]).split(",") if s.strip()]
        if names:
            where.append(f"l.source IN ({','.join('?' * len(names))})")
            params += names

    # Ordered so the best offer for each (product, shop) comes first: in stock
    # before sold out, then cheapest.
    sql = f"""
        SELECT p.id AS product_id, p.title,
               COALESCE(l.image_url, p.image_url) AS image_url,
               p.brand, p.series, p.pack_size,
               l.source, l.price, l.in_stock, l.url
          FROM products p JOIN listings l ON l.product_id = p.id
         WHERE {' AND '.join(where)}
      ORDER BY l.in_stock DESC, l.price IS NULL, l.price ASC
    """

    # One row per (product, shop). alert_state is keyed that way, so returning
    # several listings from the same shop would make the remembered state flip
    # between them and invent a restock on every run.
    best: dict[tuple[int, str], dict[str, Any]] = {}
    for row in conn.execute(sql, params):
        best.setdefault((row["product_id"], row["source"]), dict(row))
    return list(best.values())


def _prior(conn: sqlite3.Connection, watch_id: int) -> dict[tuple[int, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT product_id, source, in_stock, price FROM alert_state WHERE watch_id = ?",
        (watch_id,),
    ).fetchall()
    return {(r["product_id"], r["source"]): r for r in rows}


def _remember(conn: sqlite3.Connection, watch_id: int, m: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO alert_state (watch_id, product_id, source, in_stock, price, notified_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(watch_id, product_id, source) DO UPDATE SET
               in_stock = excluded.in_stock,
               price = excluded.price,
               notified_at = excluded.notified_at""",
        (watch_id, m["product_id"], m["source"], int(bool(m["in_stock"])),
         m["price"], db.now()),
    )


def evaluate(conn: sqlite3.Connection, *, region: str | None = None,
             seed: bool = False,
             max_markup: float | None = None) -> list[Alert]:
    """Compare the catalogue against remembered state and return transitions.

    Always updates state, whether or not anything is emitted - otherwise the
    same transition would be reported on every subsequent run. That holds for
    the markup guard too: an item priced absurdly today is remembered, so if it
    later drops to a sane price you hear about the drop rather than nothing.
    """
    from . import queries

    out: list[Alert] = []
    baselines = queries.price_baselines(conn, region) if max_markup is not None else {}

    for watch in active_watches(conn):
        prior = _prior(conn, watch["id"])
        target = watch.get("target_price")
        stock_only = watch.get("stock_only")
        stock_only = True if stock_only is None else bool(stock_only)

        for m in matches(conn, watch, region):
            key = (m["product_id"], m["source"])
            was = prior.get(key)
            buyable = bool(m["in_stock"]) and m["price"] is not None

            if not seed:
                kind = None
                if was is None:
                    # Only announce arrivals you could actually act on.
                    if buyable or not stock_only:
                        kind = NEW
                else:
                    if buyable and not was["in_stock"]:
                        kind = BACK_IN_STOCK
                    elif (target and buyable and m["price"] <= target
                          and (was["price"] is None or was["price"] > target)):
                        kind = PRICE_DROP

                markup = queries.markup_of(
                    m["price"], m["brand"], m["series"], m["pack_size"], baselines
                ) if baselines else None

                # Priced far above what its class goes for. Still remembered
                # above, so a later drop is not swallowed - just not worth a
                # message now.
                if (kind and max_markup is not None
                        and markup is not None and markup > max_markup):
                    kind = None

                if kind:
                    out.append(Alert(
                        kind=kind, watch_id=watch["id"], watch_query=watch["query"],
                        product_id=m["product_id"], title=m["title"], source=m["source"],
                        price=m["price"], url=m["url"], in_stock=bool(m["in_stock"]),
                        target_price=target,
                        previous_price=was["price"] if was else None,
                        image_url=m.get("image_url"),
                        markup=markup,
                    ))

            _remember(conn, watch["id"], m)

    conn.commit()
    return out


def format_message(alerts: list[Alert], label: Any = None) -> str:
    """One digest message, grouped by what happened."""
    if not alerts:
        return ""
    parts: list[str] = []
    for kind in (BACK_IN_STOCK, PRICE_DROP, NEW):
        group = [a for a in alerts if a.kind == kind]
        if not group:
            continue
        parts.append(f"<b>{HEADINGS[kind]}</b> ({len(group)})")
        # Keep a digest readable; Telegram caps a message at 4096 characters.
        for a in group[:10]:
            parts.append(a.line(label))
            parts.append("")
        if len(group) > 10:
            parts.append(f"...and {len(group) - 10} more")
        parts.append("")
    return "\n".join(parts).strip()


# --------------------------------------------------------------------------
# declarative rules
# --------------------------------------------------------------------------

WATCH_FIELDS = ("query", "target_price", "brand", "realism", "series",
                "pack_min", "pack_max", "sources", "stock_only")


def sync_watchlist(conn: sqlite3.Connection, rules: list[dict[str, Any]]) -> int:
    """Make the database's rules match the ones declared in config.

    Watch rules used to live only in the database, which is not in version
    control - so a fresh checkout, or a CI runner, had none at all and
    faithfully reported that nothing matched. Declaring them in config.yaml
    makes them travel with the code.

    `note` is the identity. Editing a rule in config updates it in place, so
    the alert state keyed to it survives; dropping a rule deactivates it rather
    than deleting, keeping its history. Rules added by hand through the CLI or
    dashboard have no counterpart in config and are left alone.
    """
    if not rules:
        return 0

    existing = {r["note"]: r for r in conn.execute(
        "SELECT * FROM watchlist WHERE note IS NOT NULL"
    ).fetchall()}
    declared = {r.get("note") for r in rules if r.get("note")}
    changed = 0

    for rule in rules:
        note = rule.get("note")
        if not note:
            continue
        values = {f: rule.get(f) for f in WATCH_FIELDS}
        values["query"] = values.get("query") or "*"
        values["stock_only"] = 1 if values.get("stock_only") in (None, True) else 0

        row = existing.get(note)
        if row is None:
            cols = ", ".join(["note", *WATCH_FIELDS, "active", "created_at"])
            marks = ", ".join(["?"] * (len(WATCH_FIELDS) + 3))
            conn.execute(f"INSERT INTO watchlist ({cols}) VALUES ({marks})",
                         [note, *[values[f] for f in WATCH_FIELDS], 1, db.now()])
            changed += 1
            continue

        same = all(row[f] == values[f] for f in WATCH_FIELDS) and row["active"] == 1
        if not same:
            sets = ", ".join(f"{f} = ?" for f in WATCH_FIELDS)
            conn.execute(f"UPDATE watchlist SET {sets}, active = 1 WHERE id = ?",
                         [*[values[f] for f in WATCH_FIELDS], row["id"]])
            changed += 1

    # A rule removed from config stops firing, but its history is kept.
    for note, row in existing.items():
        if note not in declared and row["active"]:
            conn.execute("UPDATE watchlist SET active = 0 WHERE id = ?", (row["id"],))
            changed += 1

    conn.commit()
    return changed
