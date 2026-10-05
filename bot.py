#!/usr/bin/env python3
"""Telegram azkar bot (no external deps).

  python bot.py send <category>   # send a category to TELEGRAM_CHAT_ID
  python bot.py menu              # answer pending /start,/menu and button taps
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"
AZKAR = json.loads((Path(__file__).parent / "azkar.json").read_text(encoding="utf-8"))
LIMIT = 4000  # Telegram max is 4096


def call(method, **params):
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
    ).encode()
    with urllib.request.urlopen(API + method, data, timeout=60) as r:
        return json.load(r)


def render(cat):
    """Return the category as a list of messages, each under LIMIT chars."""
    c = AZKAR[cat]
    blocks = []
    for i, it in enumerate(c["items"], 1):
        head = f"{i}. " + (f"({it['note']}) " if it.get("note") else "")
        tail = f"\n🔁 التكرار: {it['count']} مرة" if it["count"] > 1 else ""
        blocks.append(f"{head}\n{it['text']}{tail}")
    msgs, cur = [], c["title"] + "\n"
    for b in blocks:
        if len(cur) + len(b) + 2 > LIMIT:
            msgs.append(cur)
            cur = ""
        cur += "\n" + b + "\n"
    msgs.append(cur)
    return msgs


def send(chat_id, cat):
    for m in render(cat):
        call("sendMessage", chat_id=chat_id, text=m)


def keyboard():
    return {"inline_keyboard": [[{"text": v["title"], "callback_data": k}] for k, v in AZKAR.items()]}


def menu(chat_id):
    call("sendMessage", chat_id=chat_id, text="📿 اختر الذكر الذي تريده:", reply_markup=keyboard())


def poll():
    updates = call("getUpdates", timeout=0)["result"]
    for u in updates:
        if "callback_query" in u:
            q = u["callback_query"]
            call("answerCallbackQuery", callback_query_id=q["id"])
            if q.get("data") in AZKAR:
                send(q["message"]["chat"]["id"], q["data"])
        elif "message" in u:
            m = u["message"]
            if (m.get("text") or "").startswith(("/start", "/menu", "/azkar")):
                menu(m["chat"]["id"])
    if updates:  # confirm so they are not delivered again
        call("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "send" and len(sys.argv) > 2 and sys.argv[2] in AZKAR:
        send(os.environ["TELEGRAM_CHAT_ID"], sys.argv[2])
    elif cmd == "menu":
        poll()
    else:
        sys.exit(f"usage: bot.py send <{'|'.join(AZKAR)}> | menu")
