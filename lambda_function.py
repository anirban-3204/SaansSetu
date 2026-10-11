"""AWS entry points.

    handler        does both jobs: an HTTP event is answered from the store, anything else is a scheduled tick
    http_handler   API only. Use this for a small zip Lambda: it needs boto3 and core/ and nothing else.
    tick_handler   the scheduled run (EventBridge, every 15 minutes). Needs pandas, lightgbm and the model files.

Settings come from environment variables:
    SAANSSETU_TABLE        DynamoDB table name (required)
    SAANSSETU_TOWNS        folder of town files            default: towns
    SAANSSETU_MODELS       folder of <station_id>/ models  default: models
    SAANSSETU_SOURCE       where readings come from        default: archive
    SAANSSETU_DATA         archive folder (source=archive) default: saanssetu_data
    SAANSSETU_CLOCK_SHIFT_DAYS  how many days back the clock runs (required when source=archive).
                                670 makes 8 Oct 2026 behave as 7 Dec 2024, 9 Oct as 8 Dec, and so on.
    SAANSSETU_MODEL_SET    final or models                 default: models for archive, final otherwise
    TELEGRAM_TOKEN         bot token; without it no messages are sent
    TELEGRAM_SECRET        secret Telegram sends with each webhook call

A manual test invoke can set the clock: {"now": "2024-12-07T06:30"}.
"""
import os
from datetime import datetime, timedelta, timezone

from core import api, telegram
from core.store import DynamoStore

IST = timezone(timedelta(hours=5, minutes=30))
_cache = {}


def _store():
    if "store" not in _cache:
        _cache["store"] = DynamoStore(os.environ["SAANSSETU_TABLE"])
    return _cache["store"]


def _bot():
    token = os.environ.get("TELEGRAM_TOKEN")
    return telegram.Bot(token) if token else None


def now_ist():
    return datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)


def http_handler(event, context=None):
    method, path, query, headers, body = api.from_lambda_event(event)
    try:
        status, out = api.handle(_store(), method, path, query, headers, body, bot=_bot(),
                                 webhook_secret=os.environ.get("TELEGRAM_SECRET"), now_iso=now_ist().isoformat(timespec="minutes"))
    except Exception as e:
        status, out = 500, {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return api.to_lambda_response(status, out)


def clock_and_source(event, env, real_now):
    """(now, mode, replay_of, source, model_set). With the archive source the clock runs a fixed number of days
    in the past, so each real day replays the next day of a past winter at the real time of day."""
    from core import ingest   # imported here so the API path never loads pandas
    kind = env.get("SAANSSETU_SOURCE", "archive")
    if kind != "archive":
        raise RuntimeError(f"SAANSSETU_SOURCE={kind} is not set up. Only 'archive' exists until the live pollutant feed is chosen.")
    source = ingest.ArchiveSource(env.get("SAANSSETU_DATA", "saanssetu_data"))
    if event.get("now"):
        now = datetime.fromisoformat(event["now"])
    else:
        shift = int(env.get("SAANSSETU_CLOCK_SHIFT_DAYS") or 0)
        if shift <= 0:
            raise RuntimeError("SAANSSETU_CLOCK_SHIFT_DAYS is required with the archive source")
        now = real_now - timedelta(days=shift)
    return now, "replay", now.date().isoformat(), source, env.get("SAANSSETU_MODEL_SET", "models")


def tick_handler(event=None, context=None):
    from core import runner, townconfig
    event = event or {}
    towns, problems = townconfig.load_towns(os.environ.get("SAANSSETU_TOWNS", "towns"))
    now, mode, replay_of, source, model_set = clock_and_source(event, os.environ, now_ist())
    store, bot = _store(), _bot()
    notify = (lambda town, run: telegram.notify(store, bot, town, run)) if bot else None
    result = runner.run_all(store, towns, os.environ.get("SAANSSETU_MODELS", "models"), source, now,
                            mode=mode, model_set=model_set, notify=notify, replay_of=replay_of)
    out = {"now": now.isoformat(timespec="minutes"), "mode": mode, "towns": result, "town_file_problems": problems}
    print(out)   # one line per tick in CloudWatch
    return out


def handler(event, context=None):
    event = event or {}
    if "requestContext" in event or "httpMethod" in event or "rawPath" in event:
        return http_handler(event, context)
    return tick_handler(event, context)
