"""What a GitHub runner actually receives when it asks these shops directly.

The scraper reporting "0 items, no error" is ambiguous: the request may have
been blocked, or answered with an empty catalogue, or answered fully and then
filtered away by our own gates. Only the raw response tells them apart.

Two URLs per shop, because they differ in exactly the way that matters:
the adapter asks for `limit=250&page=1` with its own headers, while a naive
probe asking `limit=5` already came back with real data.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotwheels.sources.base import BASE_HEADERS  # noqa: E402

PLAIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def shops() -> list[tuple[str, str]]:
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    return [(s["name"], s["domain"])
            for s in cfg["sources"]["shopify"]["stores"] if s.get("enabled")]


async def hit(client: httpx.AsyncClient, url: str, headers: dict) -> str:
    try:
        r = await client.get(url, headers=headers, timeout=30,
                             follow_redirects=True)
        n = "?"
        try:
            n = str(len(r.json().get("products", [])))
        except Exception:
            n = "not json"
        return f"{r.status_code} / {len(r.content):,}b / {n} products"
    except Exception as exc:
        return f"ERR {type(exc).__name__}: {str(exc)[:40]}"


async def main() -> None:
    rows = []
    async with httpx.AsyncClient(http2=True) as client:
        for name, dom in shops():
            naive = await hit(client, f"https://{dom}/products.json?limit=5",
                              {"User-Agent": PLAIN_UA})
            # Exactly what the adapter asks for, headers included.
            adapter = await hit(client,
                                f"https://{dom}/products.json?limit=250&page=1",
                                {**BASE_HEADERS, "Referer": f"https://{dom}/"})
            rows.append(f"| {name} | {naive} | {adapter} |")

    out = "\n".join(["| shop | limit=5, plain UA | limit=250&page=1, adapter headers |",
                     "|---|---|---|"] + rows)
    print(out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Raw HTTP from the runner\n\n" + out + "\n\n")


if __name__ == "__main__":
    asyncio.run(main())
