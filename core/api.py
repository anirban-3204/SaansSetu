"""The HTTP API the dashboard reads. Every answer comes straight from the store; nothing is computed here.

    GET  /health
    GET  /towns                          town switcher: one line per town
    GET  /towns/{id}/config              station, school, routes, map geometry, bands
    GET  /towns/{id}/run/latest          the newest run
    GET  /towns/{id}/run/morning         the day's call (first full run at or after the alert hour). ?date=YYYY-MM-DD
    GET  /towns/{id}/accuracy            yesterday against actual, running score, backtest
    GET  /towns/{id}/evidence            wind-direction table for the polar plot, with its caveats
    GET  /towns/{id}/replay              dates that have a stored replay
    GET  /towns/{id}/replay/{date}       every run document of that replayed morning, in time order
    POST /telegram/webhook               Telegram calls this when someone messages the bot

This module imports neither pandas nor the models.
"""
import base64
import json
import re

from . import SCHEMA_VERSION, telegram
from .store import ACCURACY, CONFIG, EVIDENCE, LATEST, MORNING, REPLAY, TOWN

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"}
TOWN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def not_found(what):
    return 404, {"error": what}


def handle(store, method, path, query=None, headers=None, body=None, bot=None, webhook_secret=None, now_iso=None):
    """(status, body). body is a plain dict, or None for an empty answer."""
    query, headers = query or {}, {k.lower(): v for k, v in (headers or {}).items()}
    parts = [p for p in path.split("/") if p]
    if method == "OPTIONS":
        return 204, None

    if method == "POST" and parts == ["telegram", "webhook"]:
        if bot is None:
            return 503, {"error": "Telegram is not configured"}
        if webhook_secret and headers.get("x-telegram-bot-api-secret-token") != webhook_secret:
            return 403, {"error": "bad secret"}
        try:
            update = json.loads(body or "{}")
        except ValueError:
            return 400, {"error": "body is not JSON"}
        telegram.handle_update(store, bot, update, now_iso=now_iso)
        return 200, {"ok": True}

    if method != "GET":
        return 405, {"error": "method not allowed"}
    if parts == ["health"]:
        return 200, {"ok": True, "towns": len(store.query(TOWN, "all"))}
    if parts == ["towns"]:
        lines = [line for _, line in store.query(TOWN, "all")]
        lines.sort(key=lambda t: (not t.get("has_routes"), t.get("name") or ""))   # towns with routes first
        return 200, {"schema_version": SCHEMA_VERSION, "is_sample": False, "towns": lines}

    if len(parts) >= 3 and parts[0] == "towns":
        town_id, rest = parts[1], parts[2:]
        if not TOWN_ID.match(town_id) or store.get(TOWN, "all", town_id) is None:
            return not_found(f"unknown town '{town_id[:40]}'")
        if rest == ["config"]:
            doc = store.get(CONFIG, town_id, "current")
            return (200, doc) if doc else not_found("no config stored yet")
        if rest == ["run", "latest"]:
            doc = store.get(LATEST, town_id, "run")
            return (200, doc) if doc else not_found("no run yet")
        if rest == ["run", "morning"]:
            day = query.get("date")
            if day is None:
                latest = store.get(LATEST, town_id, "run")
                day = latest["clock"]["now"][:10] if latest else None
            if day is None or not DATE.match(day):
                return 400, {"error": "date must be YYYY-MM-DD"}
            doc = store.get(MORNING, town_id, day)
            return (200, doc) if doc else not_found(f"no morning call for {day}")
        if rest == ["evidence"]:
            doc = store.get(EVIDENCE, town_id, "current")
            return (200, doc) if doc else not_found("no evidence stored: run make_evidence.py, then a tick")
        if rest == ["accuracy"]:
            doc = store.get(ACCURACY, town_id, "current")
            return (200, doc) if doc else not_found("nothing scored yet")
        if rest == ["replay"]:
            dates = sorted({sort[:10] for sort, _ in store.query(REPLAY, town_id)})
            return 200, {"town_id": town_id, "dates": dates}
        if len(rest) == 2 and rest[0] == "replay":
            if not DATE.match(rest[1]):
                return 400, {"error": "date must be YYYY-MM-DD"}
            frames = [doc for _, doc in store.query(REPLAY, town_id, start=f"{rest[1]}T00:00", end=f"{rest[1]}T23:59")]
            if not frames:
                return not_found(f"no replay stored for {rest[1]}")
            return 200, {"schema_version": SCHEMA_VERSION, "town_id": town_id, "date": rest[1],
                         "times": [f["clock"]["now"][11:16] for f in frames], "frames": frames}
    return not_found("no such path")


def from_lambda_event(event):
    """(method, path, query, headers, body) from an API Gateway HTTP API or Lambda function URL event (v2),
    or a REST API event (v1)."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    path = event.get("rawPath") or event.get("path") or "/"
    body = event.get("body")
    if body and event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return method, path, event.get("queryStringParameters") or {}, event.get("headers") or {}, body


def to_lambda_response(status, body):
    headers = {**CORS, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    return {"statusCode": status, "headers": headers, "body": "" if body is None else json.dumps(body, ensure_ascii=False)}
