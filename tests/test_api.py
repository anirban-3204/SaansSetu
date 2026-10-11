"""The dashboard API and the Lambda entry points."""
import base64
import json
import subprocess
import sys

import pytest

from core import api
from core.store import SUB, TOWN, MemoryStore
from conftest import DATA, FIX, ROOT, TOWNS


def get(store, path, **kw):
    return api.handle(store, "GET", path, **kw)


def missing_keys(sample, real, path=""):
    out = []
    if isinstance(sample, dict) and isinstance(real, dict):
        for k, v in sample.items():
            if k not in real:
                out.append(f"{path}.{k}")
            elif k != "chance":
                out += missing_keys(v, real[k], f"{path}.{k}")
    elif isinstance(sample, list) and isinstance(real, list) and sample and real:
        out += missing_keys(sample[0], real[0], f"{path}[]")
    return out


def test_towns_lists_every_town_routed_first(world, replayed):
    store, _ = replayed
    status, body = get(store, "/towns")
    assert status == 200 and body["is_sample"] is False
    ids = [t["town_id"] for t in body["towns"]]
    assert set(ids) == set(world.towns) and ids[0] == world.hero["town_id"]
    for t in body["towns"]:
        for key in ["town_id", "name", "station", "has_routes", "now", "official_24h_band", "alert_today", "status", "model", "school_flag"]:
            assert key in t, key
        assert set(t["model"]) == {"level", "chance", "chance_label"}


def test_every_town_answers_every_endpoint(world, replayed):
    store, frames = replayed
    for town_id, town in world.towns.items():
        status, cfg = get(store, f"/towns/{town_id}/config")
        assert status == 200 and cfg["town_id"] == town_id and ("segments_geojson" in cfg) == town["has_routes"]
        status, latest = get(store, f"/towns/{town_id}/run/latest")
        assert status == 200 and latest == frames[town_id][-1]
        status, acc = get(store, f"/towns/{town_id}/accuracy")
        assert status == 200 and acc["town_id"] == town_id and set(acc) >= {"yesterday", "running", "daily", "backtest"}
        assert get(store, f"/towns/{town_id}/replay")[1] == {"town_id": town_id, "dates": [world.alert_day]}
        status, rep = get(store, f"/towns/{town_id}/replay/{world.alert_day}")
        assert status == 200 and rep["frames"] == frames[town_id] and rep["times"][0] == "05:00" and rep["times"] == sorted(rep["times"])
    hero = world.hero["town_id"]
    status, morning = get(store, f"/towns/{hero}/run/morning")
    assert status == 200 and morning["clock"]["now"][:10] == world.alert_day
    assert get(store, f"/towns/{hero}/run/morning", query={"date": world.alert_day})[1] == morning
    assert get(store, f"/towns/{hero}/run/morning", query={"date": "2020-01-01"})[0] == 404
    assert get(store, f"/towns/{hero}/run/morning", query={"date": "yesterday"})[0] == 400


def test_a_real_run_still_has_every_field_the_dashboard_sample_had(world, replayed):
    store, frames = replayed
    sent = next(r for r in frames[world.hero["town_id"]] if r["alert"]["decision"] == "send")
    sample = json.loads((FIX / "run.alert.json").read_text(encoding="utf-8"))
    assert missing_keys(sample, sent) == []
    for town in world.others:     # a forecast-only run has the same top-level shape, with the route parts empty
        run = frames[town["town_id"]][5]
        assert set(sample) <= set(run) and run["routes"] == [] and run["departures"] is None


def test_unknown_things_get_clear_answers():
    store = MemoryStore()
    assert get(store, "/towns") == (200, {"schema_version": api.SCHEMA_VERSION, "is_sample": False, "towns": []})
    assert get(store, "/health") == (200, {"ok": True, "towns": 0})
    assert get(store, "/towns/asansol/run/latest")[0] == 404 and "unknown town" in get(store, "/towns/asansol/run/latest")[1]["error"]
    store.put(TOWN, "all", "asansol", {"town_id": "asansol", "name": "Asansol", "has_routes": True})
    for path in ["/towns/asansol/run/latest", "/towns/asansol/config", "/towns/asansol/accuracy", "/towns/asansol/replay/2025-01-14",
                 "/towns/asansol/nope", "/nope", "/towns/asansol/run/morning?"]:
        status, body = get(store, path)
        assert status in (400, 404) and body["error"], path
    assert get(store, "/towns/asansol/replay/14-01-2025")[0] == 400
    assert get(store, "/towns/..%2F..%2Fetc/config")[0] == 404 and get(store, "/towns/A B/config")[0] == 404
    assert api.handle(store, "DELETE", "/towns")[0] == 405 and api.handle(store, "OPTIONS", "/towns") == (204, None)
    assert get(store, "/towns/asansol/replay") == (200, {"town_id": "asansol", "dates": []})


def test_replay_answers_one_date_at_a_time_in_time_order():
    store = MemoryStore()
    store.put(TOWN, "all", "asansol", {"town_id": "asansol", "name": "Asansol", "has_routes": True})
    frame = lambda stamp: {"run_id": stamp, "clock": {"now": stamp + "+05:30"}}
    for stamp in ["2024-12-07T06:30", "2024-12-07T06:00", "2024-12-08T06:00", "2024-12-06T23:45"]:
        store.put("replay", "asansol", stamp, frame(stamp))
    assert get(store, "/towns/asansol/replay")[1]["dates"] == ["2024-12-06", "2024-12-07", "2024-12-08"]
    status, body = get(store, "/towns/asansol/replay/2024-12-07")
    assert status == 200 and body["times"] == ["06:00", "06:30"] and [f["run_id"] for f in body["frames"]] == ["2024-12-07T06:00", "2024-12-07T06:30"]
    assert get(store, "/towns/asansol/replay/2024-12-09")[0] == 404


class FakeBot:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True, False


def test_telegram_webhook_checks_the_secret_and_subscribes():
    store, bot = MemoryStore(), FakeBot()
    store.put(TOWN, "all", "asansol", {"town_id": "asansol", "name": "Asansol", "has_routes": True})
    body = json.dumps({"message": {"chat": {"id": 7}, "text": "/start asansol"}})
    post = lambda **kw: api.handle(store, "POST", "/telegram/webhook", body=body, bot=bot, webhook_secret="s3cret", now_iso="t", **kw)
    assert post()[0] == 403 and post(headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})[0] == 403
    assert store.query(SUB, "asansol") == [] and bot.sent == []
    assert post(headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}) == (200, {"ok": True})
    assert [c for c, _ in store.query(SUB, "asansol")] == ["7"] and len(bot.sent) == 1
    assert api.handle(store, "POST", "/telegram/webhook", body="{not json", bot=bot)[0] == 400
    assert api.handle(store, "POST", "/telegram/webhook", body=body, bot=None)[0] == 503
    assert api.handle(store, "POST", "/towns")[0] == 405


def test_lambda_event_shapes_and_response():
    v2 = {"version": "2.0", "rawPath": "/towns/asansol/run/morning", "queryStringParameters": {"date": "2025-01-14"},
          "headers": {"x-telegram-bot-api-secret-token": "s"}, "requestContext": {"http": {"method": "GET"}}}
    assert api.from_lambda_event(v2) == ("GET", "/towns/asansol/run/morning", {"date": "2025-01-14"}, v2["headers"], None)
    v1 = {"httpMethod": "POST", "path": "/telegram/webhook", "headers": None, "queryStringParameters": None,
          "body": base64.b64encode("{\"a\": \"हवा\"}".encode()).decode(), "isBase64Encoded": True}
    assert api.from_lambda_event(v1) == ("POST", "/telegram/webhook", {}, {}, "{\"a\": \"हवा\"}")
    out = api.to_lambda_response(200, {"hi": "हवा"})
    assert out["statusCode"] == 200 and out["headers"]["Access-Control-Allow-Origin"] == "*" and "हवा" in out["body"]
    assert api.to_lambda_response(204, None)["body"] == ""


def test_the_api_side_loads_neither_pandas_nor_the_models():
    """The API Lambda must stay small: importing it may not pull in pandas, numpy, lightgbm or train."""
    code = "import sys, core.api, core.store, core.telegram, lambda_function; " \
           "bad = [m for m in ('pandas', 'numpy', 'lightgbm', 'train') if m in sys.modules]; assert not bad, bad"
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-600:]


# ---------------------------------------------------------------- the Lambda handlers against a local fake DynamoDB

@pytest.fixture
def aws(world, monkeypatch):
    pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    import lambda_function
    from core.store import create_table
    for k, v in {"AWS_DEFAULT_REGION": "ap-south-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
                 "SAANSSETU_TABLE": "saanssetu", "SAANSSETU_MODELS": str(world.root), "SAANSSETU_DATA": str(DATA),
                 "SAANSSETU_TOWNS": str(TOWNS), "SAANSSETU_SOURCE": "archive"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("SAANSSETU_CLOCK_SHIFT_DAYS", raising=False)
    with moto.mock_aws():
        create_table("saanssetu")
        lambda_function._cache.clear()
        yield lambda_function
        lambda_function._cache.clear()


def http(fn, path, method="GET"):
    out = fn.handler({"rawPath": path, "requestContext": {"http": {"method": method}}})
    return out["statusCode"], json.loads(out["body"]) if out["body"] else None


def test_scheduled_tick_then_api_read_through_dynamodb(world, aws):
    out = aws.handler({"now": f"{world.alert_day}T06:30"})
    assert out["mode"] == "replay" and out["town_file_problems"] == {} and set(out["towns"]) >= set(world.towns)
    assert out["towns"][world.hero["town_id"]] == {"status": "ok", "alert": "send", "reason": "saving_and_chance",
                                                   "steps": {"ingest": "ok", "forecast": "ok", "exposure": "ok", "departure": "ok", "alert": "sent"}}
    again = aws.handler({"now": f"{world.alert_day}T06:45"})
    assert again["towns"][world.hero["town_id"]]["alert"] == "none", "the second tick does not send again"
    status, towns = http(aws, "/towns")
    assert status == 200 and {t["town_id"] for t in towns["towns"]} >= set(world.towns)
    status, run = http(aws, f"/towns/{world.hero['town_id']}/run/latest")
    assert status == 200 and run["clock"]["now"].startswith(f"{world.alert_day}T06:45") and run["alert"]["earlier_today"]["message"]["en"]
    assert http(aws, f"/towns/{world.hero['town_id']}/run/morning")[1]["clock"]["now"].startswith(f"{world.alert_day}T06:30")
    assert http(aws, "/towns/nowhere/config")[0] == 404 and http(aws, "/towns", "OPTIONS") == (204, None)


def test_the_clock_shift_maps_today_onto_a_past_day(aws, monkeypatch):
    from datetime import datetime
    real = datetime(2026, 10, 8, 6, 30)
    with pytest.raises(RuntimeError, match="SAANSSETU_CLOCK_SHIFT_DAYS"):
        aws.clock_and_source({}, {"SAANSSETU_SOURCE": "archive"}, real)
    now, mode, replay_of, source, model_set = aws.clock_and_source({}, {"SAANSSETU_CLOCK_SHIFT_DAYS": "670"}, real)
    assert now == datetime(2024, 12, 7, 6, 30) and (mode, replay_of, source.name, model_set) == ("replay", "2024-12-07", "archive", "models")
    next_day, *_ = aws.clock_and_source({}, {"SAANSSETU_CLOCK_SHIFT_DAYS": "670"}, datetime(2026, 10, 9, 0, 15))
    assert next_day == datetime(2024, 12, 8, 0, 15)
    with pytest.raises(RuntimeError, match="not set up"):
        aws.clock_and_source({}, {"SAANSSETU_SOURCE": "openaq"}, real)


def test_a_broken_request_is_a_500_with_a_message_not_a_stack_trace(aws, monkeypatch):
    monkeypatch.setenv("SAANSSETU_TABLE", "no-such-table")
    aws._cache.clear()
    out = aws.http_handler({"rawPath": "/towns", "requestContext": {"http": {"method": "GET"}}})
    assert out["statusCode"] == 500 and "error" in json.loads(out["body"]) and out["headers"]["Access-Control-Allow-Origin"] == "*"
