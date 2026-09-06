"""Run each adapter from the runner and report what survives its own gates.

Paired with probe_raw.py: that one says whether the shop answered, this one
says whether the answer contained anything we would keep.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# This script lives in .github/, so the repo root is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels import config, sources  # noqa: E402


async def one(src, queries):
    started = time.time()
    try:
        items = await src.collect(queries)
        return src.name, len(items), time.time() - started, ""
    except Exception as exc:
        return (src.name, 0, time.time() - started,
                f"{type(exc).__name__}: {exc}"[:70])


async def main() -> None:
    cfg = config.load("config.yaml")
    srcs = [s for s in sources.build_sources(cfg) if s.name != "blinkit"]
    rows = await asyncio.gather(*(one(s, cfg.fast_queries) for s in srcs))

    lines = ["| shop | items | time | error |", "|---|---:|---|---|"]
    total = 0
    for name, n, dur, err in sorted(rows, key=lambda r: -r[1]):
        total += n
        lines.append(f"| {name} | {n} | {dur:.0f}s | {err} |")
    lines.append(f"\n**{total} items across {len(rows)} shops.**")

    out = "\n".join(lines)
    print(out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Shops reachable from a GitHub runner\n\n" + out + "\n")


if __name__ == "__main__":
    asyncio.run(main())
