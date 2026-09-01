"""FastAPI app: JSON API + the dashboard page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import bot, config, db, queries, regions, sources

WEB = Path(__file__).resolve().parent.parent / "web"


def _ceiling(asked: float | None, configured: float | None) -> float | None:
    """The tighter of what the UI asked for and what config allows."""
    if asked is None:
        return configured
    return asked if configured is None else min(asked, configured)


def create_app(config_path: str | None = None) -> FastAPI:
    # Held in a dict so switching region at runtime can swap it in place -
    # every handler reads state["cfg"] rather than closing over one instance.
    state = {"cfg": config.load(config_path)}
    app = FastAPI(title="1:64 Diecast Price Dashboard", version="1.0.0")

    def cfg_now() -> config.Config:
        return state["cfg"]

    def conn():
        return db.connect(state["cfg"].database)

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "region": {
                "label": cfg_now().region.label,
                "city": cfg_now().region.city,
                "pincode": cfg_now().region.pincode,
                "lat": cfg_now().region.lat,
                "lon": cfg_now().region.lon,
            },
            "max_price": cfg_now().max_price,
            "currency": cfg_now().currency,
            # Every source, bespoke and config-driven alike, so the dashboard
            # never needs a hardcoded list of shops.
            "sources": [
                {"name": s.name, "label": s.label, "kind": s.kind}
                for s in sources.build_sources(cfg_now())
            ],
        }

    @app.get("/api/regions")
    def list_regions() -> dict[str, Any]:
        c = conn()
        try:
            stored = [dict(r) for r in c.execute(
                "SELECT region, COUNT(*) n FROM listings GROUP BY region ORDER BY n DESC"
            ).fetchall()]
        finally:
            c.close()
        return {
            "current": regions.as_dict(cfg_now().region),
            "known": regions.known_regions(),
            "stored": stored,
        }

    @app.post("/api/region")
    def set_region(payload: dict[str, Any]) -> dict[str, Any]:
        """Switch delivery region and persist it to config.yaml."""
        query = (payload.get("query") or "").strip()
        if not query:
            raise HTTPException(400, "query is required (city name or pincode)")
        try:
            region = regions.resolve(query, online=bool(payload.get("online", True)))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        regions.save(region, config_path)
        # Reload so the running server serves the new region immediately;
        # otherwise the dashboard would keep showing the old city until restart.
        state["cfg"] = config.load(config_path)
        return {"ok": True, "region": regions.as_dict(region),
                "note": "Re-run a scrape to fetch prices for this region."}

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        c = conn()
        try:
            return queries.stats(c, cfg_now().region.pincode)
        finally:
            c.close()

    @app.get("/api/facets")
    def facets() -> dict[str, Any]:
        c = conn()
        try:
            return queries.facets(c, cfg_now().region.pincode)
        finally:
            c.close()

    @app.get("/api/products")
    def products(
        q: str | None = None,
        source: list[str] | None = Query(default=None),
        brand: list[str] | None = Query(default=None),
        pack: list[str] | None = Query(default=None),
        price_band: str | None = None,
        series: str | None = None,
        realism: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        in_stock: bool = False,
        oos_only: bool = False,
        sets_only: bool = False,
        sort: str = "price",
        limit: int = 60,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        c = conn()
        try:
            return queries.catalog(
                c, q=q, sources=source, brands=brand, packs=pack,
                price_band=price_band, series=series, realism=realism,
                min_price=min_price,
                # Clamp to the configured ceiling when there is one; with
                # `max_price: null` the dashboard's own filter is the only limit.
                max_price=_ceiling(max_price, cfg_now().max_price),
                in_stock_only=in_stock, oos_only=oos_only, sets_only=sets_only,
                region=cfg_now().region.pincode,
                sort=sort, limit=min(limit, 200), offset=max(offset, 0),
            )
        finally:
            c.close()

    @app.get("/api/products/{product_id}/history")
    def product_history(product_id: int) -> list[dict[str, Any]]:
        c = conn()
        try:
            rows = queries.history(c, product_id)
            if not rows:
                raise HTTPException(404, "no history for that product")
            return rows
        finally:
            c.close()

    @app.get("/api/deals")
    def best_deals(limit: int = 20) -> list[dict[str, Any]]:
        c = conn()
        try:
            return queries.deals(c, limit)
        finally:
            c.close()

    @app.get("/api/watchlist")
    def get_watchlist() -> list[dict[str, Any]]:
        c = conn()
        try:
            return queries.watchlist(c)
        finally:
            c.close()

    @app.post("/api/watchlist")
    def add_watch(payload: dict[str, Any]) -> dict[str, Any]:
        query = (payload.get("query") or "").strip()
        if not query:
            raise HTTPException(400, "query is required")
        c = conn()
        try:
            cur = c.execute(
                "INSERT INTO watchlist (query, note, target_price, created_at) VALUES (?,?,?,?)",
                (query, payload.get("note"), payload.get("target_price"), db.now()),
            )
            c.commit()
            return {"id": cur.lastrowid, "query": query}
        finally:
            c.close()

    @app.delete("/api/watchlist/{watch_id}")
    def remove_watch(watch_id: int) -> dict[str, bool]:
        c = conn()
        try:
            c.execute("UPDATE watchlist SET active = 0 WHERE id = ?", (watch_id,))
            c.commit()
            return {"ok": True}
        finally:
            c.close()

    @app.get("/api/bot")
    def bot_settings() -> dict[str, Any]:
        """Everything the Bot tab needs: command toggles and the mute list."""
        c = conn()
        try:
            return {
                "commands": bot.command_table(c),
                "muted_sources": sorted(bot.muted_sources(c)),
                "paused_until": db.get_state(c, bot.PAUSED_UNTIL) or None,
                "paused": bot.is_paused(c),
            }
        finally:
            c.close()

    @app.post("/api/bot/commands/{name}")
    def toggle_command(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in bot.REGISTRY:
            raise HTTPException(404, f"no such command: {name}")
        if bot.REGISTRY[name].always_on:
            raise HTTPException(400, f"/{name} cannot be switched off")
        c = conn()
        try:
            bot.set_enabled(c, name, bool(payload.get("enabled")))
            return {"name": name, "enabled": bot.enabled(c, name)}
        finally:
            c.close()

    @app.post("/api/bot/filter")
    def set_filter(payload: dict[str, Any]) -> dict[str, Any]:
        """Replace the muted-shop list wholesale."""
        names = payload.get("muted_sources")
        if not isinstance(names, list):
            raise HTTPException(400, "muted_sources must be a list")
        known = {s.name for s in sources.build_sources(cfg_now())}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise HTTPException(400, f"unknown shop(s): {', '.join(unknown)}")
        c = conn()
        try:
            bot.set_muted_sources(c, [str(n) for n in names])
            return {"muted_sources": sorted(bot.muted_sources(c))}
        finally:
            c.close()

    @app.post("/api/bot/pause")
    def set_pause(payload: dict[str, Any]) -> dict[str, Any]:
        hours = payload.get("hours")
        c = conn()
        try:
            bot.set_paused(c, float(hours) if hours else None)
            return {"paused": bot.is_paused(c),
                    "paused_until": db.get_state(c, bot.PAUSED_UNTIL) or None}
        finally:
            c.close()

    @app.get("/api/runs")
    def runs(limit: int = 25) -> list[dict[str, Any]]:
        c = conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()]
        finally:
            c.close()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.exception_handler(404)
    def not_found(_request, exc):  # type: ignore[no-untyped-def]
        return JSONResponse({"detail": str(exc.detail)}, status_code=404)

    return app


app = create_app()
