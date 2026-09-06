"""Bot commands, mute settings, and the keep/drop policy.

Three things here are easy to get subtly wrong and expensive to notice:

  * a disabled command must be invisible, not merely refused - if it still
    appears in the menu the bot advertises something it will not do;
  * muting must suppress *delivery* only. If it filtered before evaluation,
    alert state would stop advancing and unmuting would dump every change
    that happened while the shop was quiet;
  * the fantasy filter must not eat multipacks, which have no realism at all;
  * a photo is a nicety and a missed restock is not, so every failure in the
    picture path must still deliver the text digest.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import bot, config, db, normalize, notify, queries  # noqa: E402


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


@dataclass
class FakeAlert:
    source: str
    image_url: str | None = None
    kind: str = "new"
    title: str = "Some Casting"
    price: float | None = 199.0
    url: str | None = "https://example.com/p"
    previous_price: float | None = None


class FakeTelegram:
    """Records what would have been sent, and can be told to fail."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail = fail

    def send(self, text, **kw):
        self.calls.append(("text", len(text)))

    def send_photo(self, url, caption):
        if self.fail == "photo":
            raise notify.NotifyError("simulated")
        self.calls.append(("photo", url))

    def send_album(self, items):
        if self.fail == "album":
            raise notify.NotifyError("simulated")
        if not 2 <= len(items) <= 10:
            raise notify.NotifyError(f"bad album size {len(items)}")
        self.calls.append(("album", len(items)))


def pics(n: int, with_image: bool = True) -> list[FakeAlert]:
    return [FakeAlert("firstcry",
                      "https://cdn.shopify.com/x.jpg" if with_image else None)
            for _ in range(n)]


LABELS = {"firstcry": "FirstCry", "blinkit": "Blinkit", "crossword": "Crossword"}


def main() -> int:
    cfg = config.load()
    cfg.database = Path(tempfile.mkdtemp()) / "t.db"
    ok = True

    with db.session(cfg.database) as conn:
        print("defaults")
        table = bot.command_table(conn)
        on = [c["name"] for c in table if c["enabled"]]
        ok &= check("eight commands ship enabled", len(on), 8)
        ok &= check("/help is one of them", "help" in on, True)
        ok &= check("/pause ships off", bot.enabled(conn, "pause"), False)
        ok &= check("menu advertises only enabled ones",
                    len(bot.menu(conn)), len(on))

        print("toggling")
        bot.set_enabled(conn, "pause", True)
        ok &= check("/pause can be switched on", bot.enabled(conn, "pause"), True)
        ok &= check("menu grew by one", len(bot.menu(conn)), len(on) + 1)
        bot.set_enabled(conn, "help", False)
        ok &= check("/help ignores being switched off",
                    bot.enabled(conn, "help"), True)

        print("dispatch")
        ok &= check("plain chatter is not a command",
                    bot.dispatch(conn, cfg, "hello there", LABELS), None)
        ok &= check("@mention suffix is stripped",
                    bot.dispatch(conn, cfg, "/help@PreygleHWBot", LABELS)
                    .startswith("<b>Diecast watchdog</b>"), True)
        bot.set_enabled(conn, "top", False)
        ok &= check("a disabled command says so, and does not run",
                    "switched off" in bot.dispatch(conn, cfg, "/top", LABELS), True)
        ok &= check("an unknown command is refused",
                    "Unknown command" in bot.dispatch(conn, cfg, "/nope", LABELS), True)

        print("shop filter")
        ok &= check("nothing muted to begin with", bot.muted_sources(conn), set())
        bot.dispatch(conn, cfg, "/filter off blinkit", LABELS)
        ok &= check("mute takes", bot.muted_sources(conn), {"blinkit"})
        ok &= check("an unknown shop is refused, and changes nothing",
                    ("Unknown shop" in bot.dispatch(conn, cfg, "/filter off ebay", LABELS),
                     bot.muted_sources(conn)),
                    (True, {"blinkit"}))
        bot.dispatch(conn, cfg, "/filter on blinkit", LABELS)
        ok &= check("unmute takes", bot.muted_sources(conn), set())

        print("suppression")
        batch = [FakeAlert("firstcry"), FakeAlert("blinkit")]
        bot.set_muted_sources(conn, ["blinkit"])
        kept, held = bot.suppress(conn, batch)
        ok &= check("muted shop's alert is dropped",
                    ([a.source for a in kept], held), (["firstcry"], None))
        kept, held = bot.suppress(conn, [FakeAlert("blinkit")])
        ok &= check("an all-muted batch explains itself",
                    (kept, held is not None), ([], True))
        bot.set_muted_sources(conn, [])

        print("pause")
        bot.set_paused(conn, 2)
        kept, held = bot.suppress(conn, batch)
        ok &= check("pause holds everything, whatever the shop",
                    (kept, "paused" in (held or "")), ([], True))
        bot.set_paused(conn, None)
        kept, held = bot.suppress(conn, batch)
        ok &= check("resume releases", (len(kept), held), (2, None))

    print("thumbnails")
    ok &= check("shopify gets a width parameter",
                notify.thumb_url("https://cdn.shopify.com/s/f/a.jpg?v=1"),
                "https://cdn.shopify.com/s/f/a.jpg?v=1&width=320")
    ok &= check("blinkit gets a cloudflare resize prefix",
                notify.thumb_url("https://cdn.grofers.com/da/p.png"),
                "https://cdn.grofers.com/cdn-cgi/image/f=auto,w=320,q=60/da/p.png")
    ok &= check("firstcry is normalised to a small variant",
                notify.thumb_url(
                    "https://cdn.fcglcdn.com/brainbees/images/products/900x900/1a.jpg"),
                "https://cdn.fcglcdn.com/brainbees/images/products/219x265/1a.jpg")
    ok &= check("an unknown host is passed through untouched",
                notify.thumb_url("https://example.com/x.jpg"),
                "https://example.com/x.jpg")
    ok &= check("no image stays no image", notify.thumb_url(None), None)

    print("photo delivery")
    # The reported shape is what the send log records, so it has to describe
    # what actually went out - not what was planned.
    shapes = [
        ("one photo, no text needed", pics(1), [("photo",)], "photo"),
        ("a small batch is one album", pics(4), [("album",)], "album"),
        ("a big batch keeps the text digest", pics(12),
         [("text",), ("album",)], "text+album"),
        ("no images at all is text only", pics(3, False), [("text",)], "text"),
        ("a mixed batch keeps the text digest",
         pics(2) + pics(2, False), [("text",), ("album",)], "text+album"),
    ]
    for why, batch, want, want_shape in shapes:
        tg = FakeTelegram()
        _, shape = bot.send_with_photos(tg, batch, "digest", None)
        ok &= check(why, ([(c[0],) for c in tg.calls], shape), (want, want_shape))

    tg = FakeTelegram()
    bot.send_with_photos(tg, pics(12), "digest", None)
    ok &= check("a 12-photo batch still sends a legal album of 10",
                next(c[1] for c in tg.calls if c[0] == "album"), 10)

    print("photo failure never loses the alert")
    for mode in ("album", "photo"):
        tg = FakeTelegram(fail=mode)
        sent, shape = bot.send_with_photos(
            tg, pics(4 if mode == "album" else 1), "digest", None)
        ok &= check(f"{mode} failure falls back to text",
                    ([c[0] for c in tg.calls], sent, shape),
                    (["text"], ["telegram"], "text"))

    print("send log")
    with db.session(cfg.database) as conn:
        empty = db.send_summary(conn)
        ok &= check("an empty log reports no last send",
                    (empty["last"], empty["messages"]), (None, 0))

        db.log_send(conn, channel="telegram", shape="album", alerts=4,
                    kinds="new=4", ok=True)
        db.log_send(conn, channel="telegram", shape="text", alerts=2,
                    kinds="price_drop=2", ok=False, error="network down")

        summ = db.send_summary(conn)
        ok &= check("both attempts counted", summ["messages"], 2)
        ok &= check("alerts are totalled", summ["alerts"], 6)
        ok &= check("a failed send is counted as a failure", summ["failures"], 1)
        ok &= check("the newest send is the one reported",
                    (summ["last"]["shape"], bool(summ["last"]["ok"])),
                    ("text", False))
        ok &= check("recent sends come back newest first",
                    [r["shape"] for r in db.recent_sends(conn)], ["text", "album"])

    print("markup guard")
    # MRP is declared, not inferred. Inferring it from live listings is what
    # broke: the median of Hot Wheels mainline singles read Rs 179 across four
    # shops and Rs 499 across seventeen, so the guard quietly started accepting
    # a Rs 499 mainline as normal.
    declared = {"Hot Wheels": 179.0, "Hot Wheels/Premium": 549.0,
                "Matchbox": 199.0}
    ok &= check("brand MRP is used",
                queries.declared_mrp(declared, "Hot Wheels", None), 179.0)
    ok &= check("Brand/Series beats Brand",
                queries.declared_mrp(declared, "Hot Wheels", "Premium"), 549.0)
    ok &= check("a series with no entry falls back to the brand",
                queries.declared_mrp(declared, "Hot Wheels", "Mainline"), 179.0)
    ok &= check("an undeclared brand has no MRP",
                queries.declared_mrp(declared, "Tomica", None), None)

    base = {("Hot Wheels", None, 1): 179.0,
            ("Hot Wheels", "Premium", 1): 549.0,
            ("Hot Wheels", None, 5): 895.0}
    cases = [
        (150, None, 1, 0.0, "below MRP is no markup"),
        (179, None, 1, 0.0, "at MRP is no markup"),
        (199, None, 1, 0.1117, "Rs 199 on a Rs 179 car is a little over"),
        (499, None, 1, 1.7877, "Rs 499 on a Rs 179 car is extreme"),
        (549, "Premium", 1, 0.0, "a Premium is judged against Premium money"),
        (899, None, 5, 0.0045, "a five-pack is judged at five times MRP"),
    ]
    for price, series, pack, want, why in cases:
        got = queries.markup_of(price, "Hot Wheels", series, pack, base)
        ok &= check(why, round(got, 4) if got is not None else None, want)

    print("  (the cap decides what that means)")
    for price, cap, want, why in [
        (199, 0.25, True, "Rs 199 alerts at a 25% cap"),
        (224, 0.25, False, "Rs 224 is just past a 25% cap"),
        (499, 0.25, False, "Rs 499 never alerts at a 25% cap"),
        (499, 2.0, True, "a 200% cap would let Rs 499 through"),
    ]:
        mk = queries.markup_of(price, "Hot Wheels", None, 1, base)
        ok &= check(why, mk <= cap, want)

    ok &= check("an unknown class is not judged",
                queries.markup_of(999, "Tomica", None, 1, base), None)
    ok &= check("a missing price is not judged",
                queries.markup_of(None, "Hot Wheels", None, 1, base), None)

    print("  (a thin, undeclared class is never judged)")
    with db.session(cfg.database) as conn:
        ok &= check("an empty catalogue yields no baselines",
                    queries.price_baselines(conn, None, {}), {})

    print("keep policy")
    cases = [
        ("Hot Wheels 70 Ford Escort Die Cast Toy Car", True, "a real marque"),
        ("Hot Wheels Twin Mill", False, "a fantasy casting"),
        ("Hot Wheels 5 Car Pack Assortment", True, "a multipack has no realism"),
        ("Hot Wheels Licensed Play Ball 9 Inch", False, "branded non-car"),
        ("Cetaphil Sun SPF 50", False, "not diecast at all"),
    ]
    for title, want, why in cases:
        got = normalize.is_wanted(title, "Hot Wheels", exclude_fantasy=True)
        ok &= check(why, got, want)

    print("  (with the filter off, fantasy is kept)")
    ok &= check("Twin Mill survives when fantasy is allowed",
                normalize.is_wanted("Hot Wheels Twin Mill", "Hot Wheels",
                                    exclude_fantasy=False), True)

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
