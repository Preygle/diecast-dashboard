"""What a GitHub runner actually receives when it asks these shops directly.

The scraper reporting "0 items, no error" is ambiguous: the request may have
been blocked, or answered with an empty catalogue, or answered fully and then
filtered away by our own gates. Only the raw response tells them apart.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import yaml

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def targets() -> list[tuple[str, str]]:
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    out = [(s["name"], f"https://{s['domain']}/products.json?limit=5")
           for s in cfg["sources"]["shopify"]["stores"] if s.get("enabled")]
    out.append(("firstcry", "https://www.firstcry.com/search?q=hot+wheels"))
    return out


async def probe(client: httpx.AsyncClient, name: str, url: str) -> str:
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=30,
                             follow_redirects=True)
        head = r.text[:90].replace("\n", " ").replace("|", "/")
        return f"| {name} | {r.status_code} | {len(r.content):,} | `{head}` |"
    except Exception as exc:
        return f"| {name} | ERR | 0 | {type(exc).__name__}: {str(exc)[:60]} |"


async def main() -> None:
    async with httpx.AsyncClient(http2=True) as client:
        rows = await asyncio.gather(
            *(probe(client, n, u) for n, u in targets())
        )
    out = "\n".join(["| shop | HTTP | bytes | first bytes |",
                     "|---|---|---:|---|"] + rows)
    print(out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Raw HTTP from the runner\n\n" + out + "\n\n")


if __name__ == "__main__":
    asyncio.run(main())
