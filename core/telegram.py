"""Telegram: people subscribe to a town with /start, and get at most one message on a bad-air morning.

The message is the fixed template text run_once() produced. Nothing here writes its own advice.
"""
import json
import urllib.error
import urllib.request

from .store import SUB, TOWN

FOOTER = {"hi": "यह स्टेशन के पूर्वानुमान से निकाला गया अनुमान है, सड़क पर लिया गया माप नहीं।",
          "en": "This is an estimate from the station forecast, not a measurement on the street."}


def http_post(url, payload, timeout=10):
    """(HTTP status, parsed body). Telegram answers errors with JSON too, so those are returned, not raised."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {}


class Bot:
    def __init__(self, token, post=http_post):
        self._url, self._post = f"https://api.telegram.org/bot{token}/sendMessage", post

    def send(self, chat_id, text):
        """(delivered, gone). gone means this person blocked the bot or the chat no longer exists."""
        try:
            status, body = self._post(self._url, {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        except Exception:   # network trouble: not delivered, but keep the subscriber
            return False, False
        if status == 200 and body.get("ok"):
            return True, False
        gone = status == 403 or (status == 400 and "chat not found" in str(body.get("description", "")).lower())
        return False, gone


def alert_text(town, run):
    m = run["alert"]["message"]
    when = run["clock"]["now"][11:16].lstrip("0")
    tag = " (replay)" if run["mode"] == "replay" else ""
    return f"SaansSetu · {town['name']} · {when}{tag}\n\n{m['hi']}\n\n{m['en']}\n\n{FOOTER['hi']}\n{FOOTER['en']}"


def notify(store, bot, town, run):
    """Sends the run's alert to every subscriber of the town. Returns how many were delivered."""
    text, delivered = alert_text(town, run), 0
    for chat_id, _ in store.query(SUB, town["town_id"]):
        ok, gone = bot.send(chat_id, text)
        delivered += ok
        if gone:
            store.delete(SUB, town["town_id"], chat_id)
    return delivered


def handle_update(store, bot, update, now_iso=None):
    """One incoming Telegram update. /start <town> subscribes, /stop leaves. Returns the reply that was sent."""
    message = update.get("message") or {}
    chat_id, text = (message.get("chat") or {}).get("id"), (message.get("text") or "").strip()
    if chat_id is None or not text:
        return None
    towns = {tid: line for tid, line in store.query(TOWN, "all")}
    with_alerts = sorted(tid for tid, line in towns.items() if line.get("has_routes"))
    command, _, arg = text.partition(" ")
    command, arg = command.split("@")[0].lower(), arg.strip().lower()

    if command == "/start":
        if not arg and len(with_alerts) == 1:
            arg = with_alerts[0]
        if arg in with_alerts:
            store.put(SUB, arg, chat_id, {"chat_id": chat_id, "joined_at": now_iso})
            name = towns[arg]["name"]
            reply = (f"{name}: खराब हवा वाली सुबह आपको ज़्यादा से ज़्यादा एक संदेश मिलेगा। बंद करने के लिए /stop भेजें।\n\n"
                     f"{name}: you will get at most one message on a bad-air morning. Send /stop to leave.")
        elif arg in towns:
            reply = f"{towns[arg]['name']} has a forecast but no route alerts yet. Towns with alerts: {', '.join(with_alerts) or 'none yet'}."
        else:
            reply = "Send /start followed by a town. Towns with alerts: " + (", ".join(with_alerts) or "none yet") + "."
    elif command == "/stop":
        for tid in towns:
            store.delete(SUB, tid, chat_id)
        reply = "आपको अब संदेश नहीं मिलेंगे।\n\nYou will get no more messages. Send /start to join again."
    else:
        reply = "SaansSetu sends one message on a bad-air school morning. /start <town> to join, /stop to leave."
    bot.send(chat_id, reply)
    return reply
