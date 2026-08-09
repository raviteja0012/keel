"""Telegram + Discord notifier. Fire-and-forget with a small worker queue
so a slow API can never block the engine. Every message goes to every
enabled channel (Telegram chat, Discord webhook)."""
import html as _html
import queue
import re
import threading

import requests

import storage

_q: "queue.Queue[str]" = queue.Queue(maxsize=200)
_started = False

# Header prepended to every message so SLC notifications are
# distinguishable from other strategies sharing the same Telegram bot.
HEADER = "⚡ <b>[SLC BOT]</b>\n"


def _creds():
    token = storage.get_setting("telegram_bot_token", "")
    chat_id = storage.get_setting("telegram_chat_id", "")
    enabled = storage.get_setting("telegram_enabled", False)
    return token, chat_id, enabled


def _discord_creds():
    url = storage.get_setting("discord_webhook_url", "")
    enabled = storage.get_setting("discord_enabled", False)
    return url, enabled


def _to_markdown(msg: str) -> str:
    """Convert the Telegram-HTML messages to Discord markdown."""
    msg = (msg.replace("<b>", "**").replace("</b>", "**")
              .replace("<i>", "*").replace("</i>", "*")
              .replace("<code>", "`").replace("</code>", "`"))
    msg = re.sub(r"<[^>]+>", "", msg)        # strip any remaining tags
    return _html.unescape(msg)


def _send_telegram(msg: str) -> None:
    token, chat_id, enabled = _creds()
    if not (enabled and token and chat_id):
        return
    try:
        requests.post(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print("telegram error:", e)


def _send_discord(msg: str) -> None:
    url, enabled = _discord_creds()
    if not (enabled and url):
        return
    try:
        requests.post(url, json={"content": _to_markdown(msg)[:1900]}, timeout=10)
    except Exception as e:
        print("discord error:", e)


def _worker():
    while True:
        msg = _q.get()
        _send_telegram(msg)
        _send_discord(msg)


def start():
    global _started
    if not _started:
        threading.Thread(target=_worker, daemon=True).start()
        _started = True


def send(msg: str) -> None:
    try:
        _q.put_nowait(HEADER + msg)
    except queue.Full:
        pass


def send_test() -> dict:
    """Synchronous test for the dashboard button — tests every configured
    channel and reports per-channel results."""
    results = []

    token, chat_id, _ = _creds()
    if token and chat_id:
        try:
            r = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % token,
                json={"chat_id": chat_id, "parse_mode": "HTML",
                      "text": HEADER + "✅ <b>Keel</b> — Telegram connected."},
                timeout=10,
            )
            j = r.json()
            results.append("Telegram ✅" if j.get("ok")
                           else "Telegram ❌ %s" % j.get("description", "error"))
        except Exception as e:
            results.append("Telegram ❌ %s" % e)

    url, _ = _discord_creds()
    if url:
        try:
            r = requests.post(url, json={
                "content": "⚡ **[SLC BOT]**\n✅ **Keel** — Discord connected."},
                timeout=10)
            results.append("Discord ✅" if r.status_code in (200, 204)
                           else "Discord ❌ HTTP %s" % r.status_code)
        except Exception as e:
            results.append("Discord ❌ %s" % e)

    if not results:
        return {"ok": False, "error": "Fill in a Telegram token/chat id or a Discord webhook URL first."}
    ok = all("✅" in x for x in results)
    return {"ok": ok, "detail": " | ".join(results),
            **({} if ok else {"error": " | ".join(results)})}
