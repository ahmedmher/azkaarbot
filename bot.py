#!/usr/bin/env python3
"""Telegram azkar bot (stdlib only). State lives in data/state.json (committed by the workflow).

  python bot.py send <category> [--all]   # scheduled send (subscribers who enabled it; --all = every active user)
  python bot.py menu                      # long-poll Telegram for a few minutes and serve users/admin
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "state.json"
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN = int(os.environ.get("ADMIN_ID", "552728145"))
API = f"https://api.telegram.org/bot{TOKEN}/"
SCHEDULED = ["morning", "evening", "sleep"]  # categories users can subscribe to
LIMIT = 4000  # Telegram max is 4096

dirty = False


# ---------- state ----------
def load():
    s = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if "azkar" not in s:  # first run: seed from azkar.json
        seed = json.loads((ROOT / "azkar.json").read_text(encoding="utf-8"))
        s["azkar"] = {k: {"title": v["title"], "items": v["items"], "hidden": False} for k, v in seed.items()}
    s.setdefault("users", {})    # {chat_id: {"prefs": [...], "active": bool}} - ids only, no names
    s.setdefault("pending", {})  # admin multi-step flows
    s.setdefault("seq", 0)
    return s


S = load()


def save():
    global dirty
    dirty = True


def sh(*a):
    return subprocess.run(a, cwd=ROOT, capture_output=True, text=True)


def persist():
    global dirty
    if not dirty:
        return
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(S, ensure_ascii=False, indent=1), encoding="utf-8")
    if os.environ.get("GITHUB_ACTIONS"):
        sh("git", "add", "data/state.json")
        sh("git", "commit", "-m", "chore: update bot state")
        for _ in range(3):
            if sh("git", "push").returncode == 0:
                break
            sh("git", "pull", "--rebase", "-X", "theirs")
    dirty = False


# ---------- telegram ----------
def call(method, **params):
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items() if v is not None}
    ).encode()
    for attempt in range(2):
        try:
            with urllib.request.urlopen(API + method, data, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                res = json.load(e)
            except Exception:
                return {"ok": False, "error_code": e.code}
            if e.code == 429 and attempt == 0:
                time.sleep(res.get("parameters", {}).get("retry_after", 2) + 1)
                continue
            return res
        except Exception as e:  # network hiccup
            return {"ok": False, "description": str(e)}
    return {"ok": False}


def msg(chat, text, kb=None):
    return call("sendMessage", chat_id=chat, text=text, reply_markup=kb)


def show(chat, mid, text, kb=None):
    """Edit the menu message in place (fallback: send a new one)."""
    if mid:
        r = call("editMessageText", chat_id=chat, message_id=mid, text=text, reply_markup=kb)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    msg(chat, text, kb)


def kb(*rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def deliver(targets, texts):
    """Send texts to each target; mark users who blocked the bot inactive. Returns count reached."""
    ok_count = 0
    for t in targets:
        ok = True
        for text in texts:
            r = msg(t, text)
            if not r.get("ok"):
                ok = False
                if r.get("error_code") == 403 and str(t) in S["users"]:
                    S["users"][str(t)]["active"] = False
                    save()
                break
            time.sleep(0.05)
        ok_count += ok
    return ok_count


# ---------- azkar rendering ----------
def render(key):
    c = S["azkar"][key]
    blocks = []
    for i, it in enumerate(c["items"], 1):
        head = f"{i}. " + (f"({it['note']}) " if it.get("note") else "")
        tail = f"\n🔁 التكرار: {it['count']} مرة" if it.get("count", 1) > 1 else ""
        blocks.append(f"{head}\n{it['text']}{tail}")
    msgs, cur = [], c["title"] + "\n"
    for b in blocks:
        if len(cur) + len(b) + 2 > LIMIT:
            msgs.append(cur)
            cur = ""
        cur += "\n" + b + "\n"
    msgs.append(cur)
    return msgs


def send_cat(chat, key):
    if not S["azkar"][key]["items"]:
        msg(chat, "هذا القسم فارغ حالياً.")
        return
    deliver([chat], render(key))


def active_users():
    return [i for i, u in S["users"].items() if u["active"]]


# ---------- user menus ----------
def register(chat):
    u = S["users"].get(str(chat))
    if u is None:
        S["users"][str(chat)] = {"prefs": SCHEDULED[:], "active": True}
        save()
    elif not u["active"]:
        u["active"] = True
        save()


def user_menu(chat, mid=None):
    rows = [[(v["title"], "c:" + k)] for k, v in S["azkar"].items() if not v["hidden"]]
    rows.append([("🔔 إشعاراتي (الأذكار التلقائية)", "nt")])
    if chat == ADMIN:
        rows.append([("⚙️ لوحة الأدمن", "adm")])
    show(chat, mid, "📿 اختر الذكر الذي تريده:", kb(*rows))


def nt_menu(chat, mid):
    prefs = S["users"][str(chat)]["prefs"]
    rows = [[(("✅ " if k in prefs else "⬜ ") + S["azkar"][k]["title"], "nt:" + k)] for k in SCHEDULED if k in S["azkar"]]
    rows.append([("🔙 رجوع", "menu")])
    show(chat, mid, "🔔 اضغط لتفعيل/إيقاف الأذكار التي تصلك تلقائياً في موعدها:", kb(*rows))


# ---------- admin menus ----------
def admin_menu(chat, mid=None):
    S["pending"].pop(str(chat), None)
    show(chat, mid, "⚙️ لوحة التحكم", kb(
        [("📢 بث رسالة للجميع", "bc")],
        [("📤 إرسال قسم للجميع", "sc")],
        [("📚 إدارة الأذكار والأقسام", "mg")],
        [("📊 إحصائيات", "st")],
        [("🔙 القائمة", "menu")],
    ))


def cat_list(chat, mid, op, title, extra=None):
    rows = [[(("🙈 " if v["hidden"] else "") + v["title"], f"{op}:{k}")] for k, v in S["azkar"].items()]
    if extra:
        rows.append(extra)
    rows.append([("🔙 رجوع", "adm")])
    show(chat, mid, title, kb(*rows))


def cat_panel(chat, mid, key):
    c = S["azkar"][key]
    lines = [f"{c['title']}" + ("  🙈 (مخفي عن المستخدمين)" if c["hidden"] else ""), ""]
    for i, it in enumerate(c["items"], 1):
        lines.append(f"{i}. {it['text'][:45].replace(chr(10), ' ')}… ×{it.get('count', 1)}")
    if not c["items"]:
        lines.append("(فارغ)")
    text = "\n".join(lines)[:3800]
    rows = [
        [("➕ إضافة ذكر", f"ai:{key}"), ("🗑 حذف ذكر", f"di:{key}")],
        [("✏️ تغيير الاسم", f"rn:{key}"), ("👁 إخفاء/إظهار", f"hv:{key}")],
        [("📤 إرسال للجميع", f"sc:{key}")],
    ]
    if key not in SCHEDULED:
        rows.append([("🗑 حذف القسم", f"dc:{key}")])
    rows.append([("🔙 الأقسام", "mg")])
    show(chat, mid, text, kb(*rows))


def confirm(chat, mid, text, yes):
    show(chat, mid, text, kb([("✅ تأكيد", yes), ("❌ إلغاء", "adm")]))


def ask_broadcast(chat, text):
    S["pending"][str(chat)] = {"a": "bcconfirm", "text": text}
    save()
    n = len(active_users())
    msg(chat, f"📢 معاينة الرسالة:\n\n{text}\n\nستُرسل إلى {n} مستخدم.",
        kb([("✅ إرسال للجميع", "bcok"), ("❌ إلغاء", "adm")]))


def flow(chat, p, text):
    a, uid = p["a"], str(chat)
    if a == "bc":
        ask_broadcast(chat, text)
    elif a == "newcat":
        S["seq"] += 1
        key = f"c{S['seq']}"
        S["azkar"][key] = {"title": text[:60], "items": [], "hidden": False}
        S["pending"].pop(uid)
        save()
        cat_panel(chat, None, key)
    elif a == "rename":
        S["azkar"][p["key"]]["title"] = text[:60]
        S["pending"].pop(uid)
        save()
        cat_panel(chat, None, p["key"])
    elif a == "addtext":
        S["pending"][uid] = {"a": "addcount", "key": p["key"], "text": text}
        save()
        msg(chat, "كم مرة يتكرر هذا الذكر؟ ابعت رقم (مثلاً 1 أو 33). /cancel للإلغاء.")
    elif a == "addcount":
        if not (text.isdigit() and 1 <= int(text) <= 1000):
            msg(chat, "ابعت رقماً من 1 إلى 1000.")
            return
        S["azkar"][p["key"]]["items"].append({"text": p["text"], "count": int(text)})
        S["pending"].pop(uid)
        save()
        cat_panel(chat, None, p["key"])


def admin_cb(op, parts, chat, mid):
    arg = parts[1] if len(parts) > 1 else None
    azkar = S["azkar"]
    if arg is not None and op not in ("dx",) and arg not in azkar and op not in ("nt",):
        return
    if op == "adm":
        admin_menu(chat, mid)
    elif op == "st":
        users = S["users"]
        lines = [f"👥 المستخدمون: {len(users)} (نشط: {len(active_users())})"]
        for k in SCHEDULED:
            if k in azkar:
                n = sum(1 for u in users.values() if u["active"] and k in u["prefs"])
                lines.append(f"{azkar[k]['title']}: {n}")
        show(chat, mid, "\n".join(lines), kb([("🔙 رجوع", "adm")]))
    elif op == "bc":
        S["pending"][str(chat)] = {"a": "bc"}
        save()
        show(chat, mid, "✍️ ابعت نص الرسالة التي تريد بثها لكل المستخدمين.\n/cancel للإلغاء.", kb([("❌ إلغاء", "adm")]))
    elif op == "bcok":
        p = S["pending"].get(str(chat))
        if not p or p.get("a") != "bcconfirm":
            return
        S["pending"].pop(str(chat))
        targets = active_users()
        n = deliver(targets, [p["text"]])
        save()
        show(chat, mid, f"✅ تم الإرسال إلى {n} من {len(targets)}.", kb([("🔙 رجوع", "adm")]))
    elif op == "sc":
        if arg is None:
            cat_list(chat, mid, "sc", "اختر القسم الذي تريد إرساله لكل المستخدمين:")
        else:
            confirm(chat, mid, f"إرسال «{azkar[arg]['title']}» لـ {len(active_users())} مستخدم؟", f"scok:{arg}")
    elif op == "scok":
        targets = active_users()
        n = deliver(targets, render(arg))
        save()
        show(chat, mid, f"✅ تم الإرسال إلى {n} من {len(targets)}.", kb([("🔙 رجوع", "adm")]))
    elif op == "mg":
        if arg is None:
            cat_list(chat, mid, "mg", "📚 اختر القسم لإدارته:", [("➕ قسم جديد", "mgnew")])
        else:
            cat_panel(chat, mid, arg)
    elif op == "mgnew":
        S["pending"][str(chat)] = {"a": "newcat"}
        save()
        show(chat, mid, "✍️ ابعت اسم القسم الجديد (مثلاً: 🤲 أدعية).", kb([("❌ إلغاء", "adm")]))
    elif op == "ai":
        S["pending"][str(chat)] = {"a": "addtext", "key": arg}
        save()
        show(chat, mid, "✍️ ابعت نص الذكر.", kb([("❌ إلغاء", "adm")]))
    elif op == "rn":
        S["pending"][str(chat)] = {"a": "rename", "key": arg}
        save()
        show(chat, mid, "✍️ ابعت الاسم الجديد للقسم.", kb([("❌ إلغاء", "adm")]))
    elif op == "hv":
        azkar[arg]["hidden"] = not azkar[arg]["hidden"]
        save()
        cat_panel(chat, mid, arg)
    elif op == "di":
        items = azkar[arg]["items"]
        rows = [[(f"{i}. {it['text'][:40]}", f"dx:{arg}:{i - 1}")] for i, it in enumerate(items, 1)]
        rows.append([("🔙 رجوع", f"mg:{arg}")])
        show(chat, mid, "اختر الذكر الذي تريد حذفه:", kb(*rows))
    elif op == "dx":
        key, idx = arg, int(parts[2])
        if key in azkar and 0 <= idx < len(azkar[key]["items"]):
            azkar[key]["items"].pop(idx)
            save()
        if key in azkar:
            cat_panel(chat, mid, key)
    elif op == "dc":
        if arg not in SCHEDULED:
            confirm(chat, mid, f"حذف القسم «{azkar[arg]['title']}» نهائياً؟", f"dcok:{arg}")
    elif op == "dcok":
        if arg not in SCHEDULED:
            azkar.pop(arg)
            save()
            cat_list(chat, mid, "mg", "📚 اختر القسم لإدارته:", [("➕ قسم جديد", "mgnew")])


# ---------- update handling ----------
def on_callback(q):
    m = q.get("message")
    call("answerCallbackQuery", callback_query_id=q["id"])
    if not m or m["chat"]["type"] != "private":
        return
    chat, mid, uid = m["chat"]["id"], m["message_id"], q["from"]["id"]
    register(chat)
    parts = (q.get("data") or "").split(":")
    op = parts[0]
    if op == "menu":
        user_menu(chat, mid)
    elif op == "c" and len(parts) > 1 and parts[1] in S["azkar"]:
        if not S["azkar"][parts[1]]["hidden"] or uid == ADMIN:
            send_cat(chat, parts[1])
    elif op == "nt":
        if len(parts) > 1 and parts[1] in SCHEDULED:
            prefs = S["users"][str(chat)]["prefs"]
            prefs.remove(parts[1]) if parts[1] in prefs else prefs.append(parts[1])
            save()
        nt_menu(chat, mid)
    elif uid == ADMIN:
        admin_cb(op, parts, chat, mid)


def on_message(m):
    if m["chat"]["type"] != "private":
        return
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    register(chat)
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd == "/cancel":
        S["pending"].pop(str(chat), None)
        save()
        msg(chat, "تم الإلغاء.")
    elif cmd in ("/start", "/menu", "/azkar"):
        S["pending"].pop(str(chat), None)
        user_menu(chat)
    elif chat == ADMIN and cmd == "/admin":
        admin_menu(chat)
    elif chat == ADMIN and cmd == "/broadcast":
        body = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        if body:
            ask_broadcast(chat, body)
        else:
            S["pending"][str(chat)] = {"a": "bc"}
            save()
            msg(chat, "✍️ ابعت نص الرسالة التي تريد بثها.")
    elif chat == ADMIN and text and str(chat) in S["pending"]:
        flow(chat, S["pending"][str(chat)], text)
    else:
        user_menu(chat)


def handle(u):
    if "callback_query" in u:
        on_callback(u["callback_query"])
    elif "message" in u:
        on_message(u["message"])


def run_menu(seconds):
    deadline, offset, last = time.time() + seconds, None, 0
    while time.time() < deadline:
        left = int(deadline - time.time())
        r = call("getUpdates", offset=offset, timeout=max(1, min(25, left)))
        if not r.get("ok"):
            time.sleep(3)
            continue
        for u in r["result"]:
            try:
                handle(u)
            except Exception as e:
                print("error:", repr(e), file=sys.stderr)
            offset = u["update_id"] + 1
        if dirty and time.time() - last > 20:
            persist()
            last = time.time()
    persist()
    if offset:
        call("getUpdates", offset=offset, timeout=0)  # acknowledge what we handled


def scheduled(key, everyone):
    if key not in S["azkar"] or S["azkar"][key]["hidden"] or not S["azkar"][key]["items"]:
        sys.exit(f"nothing to send for {key!r}")
    if everyone:
        targets = set(active_users())
    else:
        targets = {i for i, u in S["users"].items() if u["active"] and key in u["prefs"]}
    if os.environ.get("TELEGRAM_CHAT_ID"):  # optional extra target: channel/group
        targets.add(os.environ["TELEGRAM_CHAT_ID"])
    print(f"sent to {deliver(sorted(targets), render(key))}/{len(targets)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["send"] and len(args) > 1:
        scheduled(args[1], "--all" in args)
    elif args[:1] == ["menu"]:
        run_menu(int(os.environ.get("LOOP_SECONDS", "280")))
    else:
        sys.exit("usage: bot.py send <category> [--all] | menu")
