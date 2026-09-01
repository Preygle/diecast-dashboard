"""Bot commands, mute settings, and the keep/drop policy.

Three things here are easy to get subtly wrong and expensive to notice:

  * a disabled command must be invisible, not merely refused - if it still
    appears in the menu the bot advertises something it will not do;
  * muting must suppress *delivery* only. If it filtered before evaluation,
    alert state would stop advancing and unmuting would dump every change
    that happened while the shop was quiet;
  * the fantasy filter must not eat multipacks, which have no realism at all.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import bot, config, db, normalize  # noqa: E402


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


@dataclass
class FakeAlert:
    source: str


LABELS = {"firstcry": "FirstCry", "blinkit": "Blinkit", "crossword": "Crossword"}


def main() -> int:
    cfg = config.load()
    cfg.database = Path(tempfile.mkdtemp()) / "t.db"
    ok = True

    with db.session(cfg.database) as conn:
        print("defaults")
        table = bot.command_table(conn)
        on = [c["name"] for c in table if c["enabled"]]
        ok &= check("seven commands ship enabled", len(on), 7)
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
