"""Scrape orchestration: run sources, group listings into products, persist."""

from __future__ import annotations

import asyncio
import sqlite3
import traceback
from dataclasses import dataclass, field

from . import db, normalize
from .config import Config
from .sources import BROWSER_SOURCES, Item, Source, build_sources


@dataclass
class SourceResult:
    name: str
    label: str
    ok: bool
    items: int = 0
    stored: int = 0
    error: str | None = None


@dataclass
class RunReport:
    results: list[SourceResult] = field(default_factory=list)
    products: int = 0
    price_changes: int = 0

    @property
    def total_items(self) -> int:
        return sum(r.items for r in self.results)

    def render(self) -> str:
        lines = ["", "Scrape summary", "-" * 52]
        for r in self.results:
            status = "ok  " if r.ok else "FAIL"
            detail = f"{r.stored:4d} stored / {r.items:4d} found"
            lines.append(f"  {status} {r.label:<20} {detail}")
            if r.error:
                lines.append(f"       -> {r.error}")
        lines.append("-" * 52)
        lines.append(f"  {self.products} products, {self.price_changes} price changes")
        return "\n".join(lines)


async def _run_source(src: Source, queries: list[str]) -> tuple[SourceResult, list[Item]]:
    try:
        items = await src.collect(queries)
        return SourceResult(src.name, src.label, True, items=len(items)), items
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        return SourceResult(src.name, src.label, False, error=msg), []


def _store(conn: sqlite3.Connection, cfg: Config, items: list[Item]) -> tuple[int, int]:
    """Group items into products and write them. Returns (stored, price_changes)."""
    # Signature index of everything already known, so repeat scrapes reuse rows.
    known: dict[str, str] = {
        row["canonical_key"]: normalize.key_signature(row["canonical_key"])
        for row in db.all_products(conn)
    }

    stored = 0
    changes = 0

    # The taste filter is applied here rather than in each adapter, so one
    # config flag governs every shop and a re-run of `regroup` applies the
    # same rule to everything already on disk.
    items = [i for i in items
             if normalize.is_wanted(i.raw_title, i.brand_hint,
                                    exclude_fantasy=cfg.exclude_fantasy)]

    # Longest titles first: they carry the most tokens, which makes them better
    # anchors for the fuzzy matcher than a terse quick-commerce name.
    for item in sorted(items, key=lambda i: len(i.raw_title), reverse=True):
        title = item.raw_title
        hint = item.brand_hint
        key = normalize.find_match(title, known, cfg.match_threshold, hint=hint)
        if key is None:
            key = normalize.canonical_key(title, hint)
            known[key] = normalize.key_signature(key)

        pid = db.upsert_product(
            conn,
            canonical_key=key,
            title=title,
            brand=normalize.detect_brand(title, hint),
            series=normalize.detect_series(title),
            realism=normalize.detect_realism(title),
            pack_size=normalize.pack_size(title),
            is_set=normalize.is_set(title),
            image_url=item.image_url,
        )
        _, changed = db.upsert_listing(conn, pid, item.as_dict())
        stored += 1
        changes += int(changed)

    conn.commit()
    return stored, changes


def regroup(cfg: Config) -> tuple[int, int]:
    """Rebuild products from stored listings using the current matcher.

    Grouping rules change - a normalizer fix, a different match_threshold - and
    re-scraping every site just to re-derive them is wasteful and rude to the
    sites. Listings are the raw record; products are derived, so they can be
    rebuilt offline. Returns (products_before, products_after).
    """
    with db.session(cfg.database) as conn:
        before = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]

        # Filters tighten over time (a new brand gate, a scale check). Listings
        # stored under looser rules would otherwise linger forever, since a
        # re-scrape only refreshes SKUs it happens to see again. Re-apply the
        # current filter here so regroup also cleans.
        stale = [
            r["id"] for r in conn.execute("SELECT id, raw_title, brand_hint FROM listings")
            if not normalize.is_wanted(r["raw_title"], r["brand_hint"],
                                       exclude_fantasy=cfg.exclude_fantasy)
        ]
        if stale:
            conn.executemany("DELETE FROM listings WHERE id = ?", [(i,) for i in stale])
            print(f"  dropped {len(stale)} listing(s) no longer matching the filters")

        # Brand, series and realism are derived, and upserts keep the first
        # non-null value so a terse title cannot clobber a good one. That means
        # a classification made under an older rule would survive this rebuild
        # forever, so clear them and let the loop below derive them again.
        conn.execute("UPDATE products SET brand = NULL, series = NULL, realism = NULL")

        rows = conn.execute(
            """SELECT id, raw_title, image_url, brand_hint FROM listings
                ORDER BY length(raw_title) DESC"""
        ).fetchall()

        # Never clear `products` first: listings.product_id is ON DELETE
        # CASCADE, so emptying the parent table takes every listing (and its
        # price history) with it. Reassign listings onto freshly-derived
        # products, then drop only what nothing points at any more.
        known: dict[str, str] = {}

        for row in rows:
            title = row["raw_title"]
            hint = row["brand_hint"]
            key = normalize.find_match(title, known, cfg.match_threshold, hint=hint)
            if key is None:
                key = normalize.canonical_key(title, hint)
                known[key] = normalize.key_signature(key)

            pid = db.upsert_product(
                conn,
                canonical_key=key,
                title=title,
                brand=normalize.detect_brand(title, hint),
                series=normalize.detect_series(title),
                realism=normalize.detect_realism(title),
                pack_size=normalize.pack_size(title),
                is_set=normalize.is_set(title),
                image_url=row["image_url"],
            )
            conn.execute("UPDATE listings SET product_id = ? WHERE id = ?", (pid, row["id"]))

        conn.execute(
            "DELETE FROM products WHERE id NOT IN (SELECT DISTINCT product_id FROM listings)"
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]

    return before, after


async def scrape(cfg: Config, only: list[str] | None = None) -> RunReport:
    all_sources = build_sources(cfg, only)
    if not all_sources:
        raise SystemExit("No sources enabled or matched. Check config.yaml.")

    report = RunReport()

    with db.session(cfg.database) as conn:
        # A run killed mid-flight leaves rows with no finished_at, which would
        # otherwise read as "still running" forever in the scrape log.
        conn.execute(
            """UPDATE scrape_runs SET finished_at = ?, ok = 0,
                      error = COALESCE(error, 'interrupted')
                WHERE finished_at IS NULL""",
            (db.now(),),
        )
        conn.commit()

        # HTTP sources run concurrently; browser sources run one at a time so
        # several Chromium instances do not fight over CPU and the network.
        http_sources = [s for s in all_sources if s.name not in BROWSER_SOURCES]
        browser_sources = [s for s in all_sources if s.name in BROWSER_SOURCES]

        batches: list[list[Source]] = []
        if http_sources:
            # Specialty shops are small and independent; running them together
            # keeps a 12-store sweep to about the time of one store.
            batches.append(http_sources)
        batches.extend([s] for s in browser_sources)

        for sources in batches:
            run_ids = {s.name: db.start_run(conn, s.name) for s in sources}

            print(f"\nScraping: {', '.join(s.label for s in sources)}")
            outcomes = await asyncio.gather(
                *(_run_source(s, cfg.queries) for s in sources)
            )

            for result, items in outcomes:
                if items:
                    # Storing must not be able to kill the run. One store
                    # returning an odd payload used to abort every remaining
                    # source and lose the whole sweep.
                    try:
                        result.stored, ch = _store(conn, cfg, items)
                        report.price_changes += ch
                    except Exception as exc:
                        conn.rollback()
                        result.ok = False
                        result.error = f"store failed: {type(exc).__name__}: {exc}"
                        traceback.print_exc()
                db.finish_run(
                    conn, run_ids[result.name],
                    ok=result.ok, items=result.items, error=result.error,
                )
                report.results.append(result)
                print(f"  [{result.name}] {result.items} items"
                      f"{' within cap' if cfg.max_price else ''}")

        report.products = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]

    return report
