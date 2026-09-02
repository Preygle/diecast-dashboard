"""SQLite storage.

Two-level model: a `product` is one real-world Hot Wheels item (a car, a
5-pack, a track set). A `listing` is one site's offer for that product. The
dashboard's whole job is showing, per product, which listing is cheapest.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    brand         TEXT,
    series        TEXT,
    pack_size     INTEGER NOT NULL DEFAULT 1,
    is_set        INTEGER NOT NULL DEFAULT 0,
    image_url     TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    source_sku    TEXT NOT NULL,
    region        TEXT NOT NULL,
    url           TEXT,
    raw_title     TEXT NOT NULL,
    price         REAL NOT NULL,
    mrp           REAL,
    in_stock      INTEGER NOT NULL DEFAULT 1,
    rating        REAL,
    reviews       INTEGER,
    delivery_eta  TEXT,
    image_url     TEXT,
    first_seen    TEXT NOT NULL,
    scraped_at    TEXT NOT NULL,
    UNIQUE(source, source_sku, region)
);

CREATE INDEX IF NOT EXISTS idx_listings_product ON listings(product_id);
CREATE INDEX IF NOT EXISTS idx_listings_price   ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_source  ON listings(source);

CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price      REAL NOT NULL,
    in_stock   INTEGER NOT NULL DEFAULT 1,
    ts         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_listing ON price_history(listing_id, ts);

CREATE TABLE IF NOT EXISTS watchlist (
    id           INTEGER PRIMARY KEY,
    query        TEXT NOT NULL,
    note         TEXT,
    target_price REAL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

-- What each watch rule has already been told about, so a scheduled run only
-- reports transitions (came back in stock, dropped below target) instead of
-- re-sending the entire matching catalogue every time it fires.
CREATE TABLE IF NOT EXISTS alert_state (
    id         INTEGER PRIMARY KEY,
    watch_id   INTEGER NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL,
    source     TEXT NOT NULL,
    in_stock   INTEGER NOT NULL DEFAULT 0,
    price      REAL,
    notified_at TEXT NOT NULL,
    UNIQUE(watch_id, product_id, source)
);

CREATE INDEX IF NOT EXISTS idx_alert_watch ON alert_state(watch_id);

-- Cursor for Telegram getUpdates, so commands sent between runs are processed
-- exactly once without needing a always-on webhook listener.
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- What the bot actually sent. alert_state records what was *seen*, which is
-- not the same thing: a run can evaluate cleanly and still fail to deliver.
-- Without this, "did it alert me on Tuesday?" can only be inferred.
CREATE TABLE IF NOT EXISTS notify_log (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    channel TEXT NOT NULL,          -- telegram, discord
    shape   TEXT NOT NULL,          -- text, photo, album, text+album
    alerts  INTEGER NOT NULL DEFAULT 0,
    kinds   TEXT,                   -- "new=4, price_drop=1"
    ok      INTEGER NOT NULL DEFAULT 1,
    error   TEXT
);

CREATE INDEX IF NOT EXISTS idx_notify_ts ON notify_log(ts DESC, id DESC);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    items       INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table untouched, so new columns must be added explicitly rather than
# silently missing on databases created by an earlier version.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("products", "brand", "ALTER TABLE products ADD COLUMN brand TEXT"),
    ("listings", "brand_hint", "ALTER TABLE listings ADD COLUMN brand_hint TEXT"),
    ("products", "realism", "ALTER TABLE products ADD COLUMN realism TEXT"),
    # Watch rules grew from "a text query" into real filters.
    ("watchlist", "brand", "ALTER TABLE watchlist ADD COLUMN brand TEXT"),
    ("watchlist", "realism", "ALTER TABLE watchlist ADD COLUMN realism TEXT"),
    ("watchlist", "series", "ALTER TABLE watchlist ADD COLUMN series TEXT"),
    ("watchlist", "pack_min", "ALTER TABLE watchlist ADD COLUMN pack_min INTEGER"),
    ("watchlist", "pack_max", "ALTER TABLE watchlist ADD COLUMN pack_max INTEGER"),
    ("watchlist", "sources", "ALTER TABLE watchlist ADD COLUMN sources TEXT"),
    ("watchlist", "stock_only", "ALTER TABLE watchlist ADD COLUMN stock_only INTEGER DEFAULT 1"),
]

# Indexes on migrated columns live here, not in SCHEMA. SCHEMA runs first, and
# an index over a column the existing table does not have yet fails the whole
# script - so anything depending on a migration must come after it.
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand)",
    "CREATE INDEX IF NOT EXISTS idx_products_realism ON products(realism)",
]


LISTING_COLUMNS = (
    "id, product_id, source, source_sku, region, url, raw_title, price, mrp, "
    "in_stock, rating, reviews, delivery_eta, image_url, brand_hint, "
    "first_seen, scraped_at"
)

# Rebuild of `listings` to drop NOT NULL from price. A sold-out listing usually
# shows no price at all, and rejecting those silently discarded exactly the
# out-of-stock rows we want to record.
_NULLABLE_PRICE_REBUILD = f"""
CREATE TABLE listings_new (
    id            INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    source_sku    TEXT NOT NULL,
    region        TEXT NOT NULL,
    url           TEXT,
    raw_title     TEXT NOT NULL,
    price         REAL,
    mrp           REAL,
    in_stock      INTEGER NOT NULL DEFAULT 1,
    rating        REAL,
    reviews       INTEGER,
    delivery_eta  TEXT,
    image_url     TEXT,
    brand_hint    TEXT,
    first_seen    TEXT NOT NULL,
    scraped_at    TEXT NOT NULL,
    UNIQUE(source, source_sku, region)
);
INSERT INTO listings_new ({LISTING_COLUMNS}) SELECT {LISTING_COLUMNS} FROM listings;
DROP TABLE listings;
ALTER TABLE listings_new RENAME TO listings;
CREATE INDEX IF NOT EXISTS idx_listings_product ON listings(product_id);
CREATE INDEX IF NOT EXISTS idx_listings_price   ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_source  ON listings(source);
"""


def _price_is_not_null(conn: sqlite3.Connection) -> bool:
    for r in conn.execute("PRAGMA table_info(listings)"):
        if r["name"] == "price":
            return bool(r["notnull"])
    return False


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    for ddl in POST_MIGRATION_INDEXES:
        conn.execute(ddl)
    conn.commit()

    if _price_is_not_null(conn):
        # Foreign keys must be off for the swap, or dropping the old table
        # cascades into price_history and destroys it. Ids are carried over
        # verbatim so price_history keeps pointing at the right listings.
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript(_NULLABLE_PRICE_REBUILD)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def session(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------

def all_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, canonical_key, title, pack_size, is_set FROM products"
    ).fetchall()


def upsert_product(
    conn: sqlite3.Connection,
    *,
    canonical_key: str,
    title: str,
    brand: str | None,
    series: str | None,
    realism: str | None,
    pack_size: int,
    is_set: bool,
    image_url: str | None,
) -> int:
    ts = now()
    row = conn.execute(
        "SELECT id FROM products WHERE canonical_key = ?", (canonical_key,)
    ).fetchone()

    if row:
        pid = row["id"]
        # Keep the longest title seen; it is usually the most descriptive.
        conn.execute(
            """UPDATE products
                  SET last_seen = ?,
                      title     = CASE WHEN length(?) > length(title) THEN ? ELSE title END,
                      brand     = COALESCE(brand, ?),
                      series    = COALESCE(series, ?),
                      realism   = COALESCE(realism, ?),
                      image_url = COALESCE(image_url, ?)
                WHERE id = ?""",
            (ts, title, title, brand, series, realism, image_url, pid),
        )
        return pid

    cur = conn.execute(
        """INSERT INTO products
               (canonical_key, title, brand, series, realism, pack_size, is_set,
                image_url, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (canonical_key, title, brand, series, realism, pack_size, int(is_set),
         image_url, ts, ts),
    )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------

def upsert_listing(conn: sqlite3.Connection, product_id: int, item: dict[str, Any]) -> tuple[int, bool]:
    """Insert or update a listing. Returns (listing_id, price_changed)."""
    ts = now()
    key = (item["source"], item["source_sku"], item["region"])

    row = conn.execute(
        "SELECT id, price FROM listings WHERE source=? AND source_sku=? AND region=?", key
    ).fetchone()

    # None means "sold out, no price shown" - a real state we record rather
    # than discard.
    raw_price = item.get("price")
    price = None if raw_price is None else float(raw_price)

    if row:
        lid = row["id"]
        old = row["price"]
        if old is None or price is None:
            changed = (old is None) != (price is None)
        else:
            changed = abs(float(old) - price) > 0.001
        conn.execute(
            """UPDATE listings
                  SET product_id=?, url=?, raw_title=?, price=?, mrp=?, in_stock=?,
                      rating=?, reviews=?, delivery_eta=?, image_url=?,
                      brand_hint=COALESCE(?, brand_hint), scraped_at=?
                WHERE id=?""",
            (
                product_id, item.get("url"), item["raw_title"], price, item.get("mrp"),
                int(item.get("in_stock", True)), item.get("rating"), item.get("reviews"),
                item.get("delivery_eta"), item.get("image_url"),
                item.get("brand_hint"), ts, lid,
            ),
        )
    else:
        changed = True
        cur = conn.execute(
            """INSERT INTO listings
                   (product_id, source, source_sku, region, url, raw_title, price, mrp,
                    in_stock, rating, reviews, delivery_eta, image_url, brand_hint,
                    first_seen, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                product_id, item["source"], item["source_sku"], item["region"],
                item.get("url"), item["raw_title"], price, item.get("mrp"),
                int(item.get("in_stock", True)), item.get("rating"), item.get("reviews"),
                item.get("delivery_eta"), item.get("image_url"),
                item.get("brand_hint"), ts, ts,
            ),
        )
        lid = int(cur.lastrowid)

    # Only append history when something actually moved, so the table stays
    # meaningful instead of one row per scrape. An unpriced (sold-out) sighting
    # has no price to plot, so it is not charted.
    if changed and price is not None:
        conn.execute(
            "INSERT INTO price_history (listing_id, price, in_stock, ts) VALUES (?,?,?,?)",
            (lid, price, int(item.get("in_stock", True)), ts),
        )

    return lid, changed


# --------------------------------------------------------------------------
# scrape runs
# --------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, started_at) VALUES (?, ?)", (source, now())
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, *, ok: bool, items: int, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, ok=?, items=?, error=? WHERE id=?",
        (now(), int(ok), items, error, run_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# bot state (small key/value scratchpad)
# --------------------------------------------------------------------------

def log_send(conn: sqlite3.Connection, *, channel: str, shape: str,
             alerts: int, kinds: str | None = None, ok: bool = True,
             error: str | None = None) -> None:
    """Record one delivery attempt, successful or not."""
    conn.execute(
        """INSERT INTO notify_log (ts, channel, shape, alerts, kinds, ok, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now(), channel, shape, alerts, kinds, int(ok), error),
    )
    conn.commit()


def recent_sends(conn: sqlite3.Connection, limit: int = 25) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM notify_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()]


def send_summary(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """Last delivery and a recent tally, for /status and the dashboard."""
    last = conn.execute(
        "SELECT * FROM notify_log ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    row = conn.execute(
        """SELECT COUNT(*) AS messages,
                  COALESCE(SUM(alerts), 0) AS alerts,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures
             FROM notify_log WHERE ts >= ?""", (since,)
    ).fetchone()
    return {
        "last": dict(last) if last else None,
        "days": days,
        "messages": row["messages"],
        "alerts": row["alerts"],
        "failures": row["failures"] or 0,
    }


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO bot_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    conn.commit()
