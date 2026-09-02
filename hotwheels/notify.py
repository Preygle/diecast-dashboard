"""Outbound notifications: Telegram and Discord.

Both are plain HTTP, deliberately: neither needs a persistent gateway
connection, so a scheduled job on a laptop, a phone or GitHub Actions can send
alerts without anything running in between.

WhatsApp is not supported. It requires Meta's Business API (business
verification, per-message pricing) and the unofficial libraries reliably get
numbers banned - not a trade worth making for a price alert.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read .env into a dict, without clobbering real environment variables.

    Real env vars win, so GitHub Actions secrets override the local file.
    """
    values: dict[str, str] = {}
    f = path or ENV_FILE
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def set_env_value(key: str, value: str, path: Path | None = None) -> None:
    """Persist one key back into .env, leaving the rest of the file intact."""
    f = path or ENV_FILE
    lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    f.write_text("\n".join(out) + "\n", encoding="utf-8")


# Telegram fetches photo URLs itself and re-compresses them, so the point of
# asking a CDN for a smaller variant is not the bytes on our wire - it is that
# Telegram fetches faster and never meets its 10MB limit on a source image.
# Every shop here sits behind a CDN that resizes from the URL; anything else is
# passed through untouched.
THUMB_WIDTH = 320


def thumb_url(url: str | None, width: int = THUMB_WIDTH) -> str | None:
    """A smaller variant of a product image, where the CDN offers one."""
    if not url:
        return None
    if "cdn.shopify.com" in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}width={width}"
    if "cdn.grofers.com" in url:
        # Cloudflare image resizing, inserted between host and path.
        host, _, path = url.partition(".com/")
        if path:
            return f"{host}.com/cdn-cgi/image/f=auto,w={width},q=60/{path}"
    if "cdn.fcglcdn.com" in url:
        # FirstCry encodes the size as a path segment. What we store is
        # already small; only shrink it if a larger variant was captured.
        return re.sub(r"/products/\d+x\d+/", "/products/219x265/", url)
    return url


class NotifyError(RuntimeError):
    pass


@dataclass
class Telegram:
    token: str
    chat_id: str | None = None

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            r = httpx.post(url, json=payload, timeout=30)
            data = r.json()
        except Exception as exc:
            raise NotifyError(f"telegram {method} failed: {exc}") from exc
        if not data.get("ok"):
            raise NotifyError(f"telegram {method}: {data.get('description', data)}")
        return data["result"]

    def me(self) -> dict[str, Any]:
        return self.call("getMe")

    def send(self, text: str, *, preview: bool = False) -> dict[str, Any]:
        if not self.chat_id:
            raise NotifyError("no TELEGRAM_CHAT_ID - message the bot, then run "
                              "`python cli.py notify chatid`")
        # Telegram hard-caps a message at 4096 characters.
        return self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=text[:4096],
            parse_mode="HTML",
            disable_web_page_preview=not preview,
        )

    def send_photo(self, photo_url: str, caption: str) -> dict[str, Any]:
        if not self.chat_id:
            raise NotifyError("no TELEGRAM_CHAT_ID set")
        return self.call("sendPhoto", chat_id=self.chat_id,
                         photo=photo_url, caption=caption[:1024], parse_mode="HTML")

    def send_album(self, items: list[dict[str, str]]) -> dict[str, Any]:
        """Send 2-10 captioned photos as one album.

        Telegram rejects a group of one, so a single photo goes through
        `send_photo` instead - the caller picks.
        """
        if not self.chat_id:
            raise NotifyError("no TELEGRAM_CHAT_ID set")
        if not 2 <= len(items) <= 10:
            raise NotifyError(f"an album needs 2-10 photos, got {len(items)}")
        media = [
            {"type": "photo", "media": it["url"],
             "caption": it.get("caption", "")[:1024], "parse_mode": "HTML"}
            for it in items
        ]
        return self.call("sendMediaGroup", chat_id=self.chat_id, media=media)

    def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", **payload)

    def discover_chat_id(self) -> str | None:
        """Find the chat that last messaged this bot."""
        for update in reversed(self.updates()):
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                return str(chat["id"])
        return None


@dataclass
class Discord:
    webhook_url: str

    def send(self, text: str) -> None:
        # Discord's limit is 2000 characters and it speaks Markdown, not HTML.
        body = text.replace("<b>", "**").replace("</b>", "**")
        body = body.replace("<i>", "_").replace("</i>", "_")
        try:
            r = httpx.post(self.webhook_url, json={"content": body[:2000]}, timeout=30)
        except Exception as exc:
            raise NotifyError(f"discord webhook failed: {exc}") from exc
        if r.status_code >= 300:
            raise NotifyError(f"discord webhook HTTP {r.status_code}: {r.text[:200]}")


def build(env: dict[str, str] | None = None) -> tuple[Telegram | None, Discord | None]:
    """Whichever channels are configured. Both optional, at least one needed."""
    env = env if env is not None else load_env()
    tg = Telegram(env["TELEGRAM_BOT_TOKEN"], env.get("TELEGRAM_CHAT_ID") or None) \
        if env.get("TELEGRAM_BOT_TOKEN") else None
    dc = Discord(env["DISCORD_WEBHOOK_URL"]) if env.get("DISCORD_WEBHOOK_URL") else None
    return tg, dc


def broadcast(text: str, env: dict[str, str] | None = None) -> list[str]:
    """Send to every configured channel. Returns the channels that succeeded.

    One channel failing must not stop the other: a dead Discord webhook should
    never suppress the Telegram alert.
    """
    tg, dc = build(env)
    sent: list[str] = []
    for name, sender in (("telegram", tg), ("discord", dc)):
        if sender is None:
            continue
        try:
            sender.send(text)
            sent.append(name)
        except NotifyError as exc:
            print(f"  [{name}] {exc}")
    return sent
