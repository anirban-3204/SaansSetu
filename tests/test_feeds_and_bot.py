"""Parts that talk to the outside world, tested with stand-ins: live weather, Telegram, and the nightly scoring rules."""
import io
import json
import urllib.error

import pandas as pd
import pytest

from core import check, ingest, telegram
from core.store import ALERT, AQ, MORNING, SUB, TOWN, MemoryStore
from core.townconfig import prepare

STATION = {"station_id": "site_1418", "lat": 23.685297, "lon": 86.945968}


# ---------------------------------------------------------------- live weather (Open-Meteo forecast API)

def open_meteo_answer(variables, start="2026-10-07 00:00", hours=72):
    times = pd.date_range(start, periods=hours, freq="h")
    hourly = {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times]}
    for i, v in enumerate(variables):
        hourly[v] = [float(i * 100 + k) for k in range(hours)]
    return {"latitude": 23.7, "longitude": 86.9, "timezone": "Asia/Kolkata", "hourly": hourly}


def test_live_weather_asks_like_training_did_and_returns_only_the_past():
    calls = []

    def fake(url, params=None, headers=None, timeout=20):
        calls.append((url, params))
        return open_meteo_answer(params["hourly"].split(","))

    wx = ingest.OpenMeteoWeather(fake).weather(STATION, pd.Timestamp("2026-10-08 00:00"), pd.Timestamp("2026-10-08 06:30"))
    url, params = calls[0]
    assert url == "https://api.open-meteo.com/v1/forecast"
    assert params["latitude"] == 23.7 and params["longitude"] == 86.9, "the same 0.1-degree cell fetch_data.py used"
    assert params["timezone"] == "Asia/Kolkata" and params["wind_speed_unit"] == "ms" and params["past_days"] >= 1
    assert params["hourly"].split(",") == ingest.WX_COLS
    assert list(wx.columns) == ingest.WX_COLS
    assert wx.index.min() == pd.Timestamp("2026-10-08 01:00") and wx.index.max() == pd.Timestamp("2026-10-08 06:00")
    assert wx.loc["2026-10-08 06:00", "temperature_2m"] == 30.0   # hour 30 of the answer, first variable


def test_live_weather_carries_on_when_mixing_height_is_not_offered():
    asked = []

    def fake(url, params=None, headers=None, timeout=20):
        variables = params["hourly"].split(",")
        asked.append(variables)
        if "boundary_layer_height" in variables:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b'{"error":true}'))
        return open_meteo_answer(variables)

    feed = ingest.OpenMeteoWeather(fake)
    wx = feed.weather(STATION, pd.Timestamp("2026-10-08 00:00"), pd.Timestamp("2026-10-08 06:30"))
    assert "boundary_layer_height" not in wx and "wind_direction_10m" in wx and len(asked) == 2
    feed.weather(STATION, pd.Timestamp("2026-10-08 00:00"), pd.Timestamp("2026-10-08 07:30"))
    assert len(asked) == 3, "it does not ask for mixing height again"


def test_live_weather_errors_are_raised_for_sync_to_report():
    def down(url, params=None, headers=None, timeout=20):
        raise urllib.error.HTTPError(url, 503, "Unavailable", {}, io.BytesIO(b""))
    with pytest.raises(urllib.error.HTTPError):
        ingest.OpenMeteoWeather(down).weather(STATION, pd.Timestamp("2026-10-08 00:00"), pd.Timestamp("2026-10-08 06:30"))
    with pytest.raises(ValueError):
        ingest.OpenMeteoWeather(lambda *a, **k: {"hourly": {"time": []}}).weather(STATION, pd.Timestamp("2026-10-08"), pd.Timestamp("2026-10-08 06:30"))
    town = prepare({"town_id": "asansol", "name": "Asansol", "station": STATION})
    live = ingest.LiveSource(type("NoFeed", (), {"name": "none", "pollutants": lambda self, *a: pd.DataFrame()})(), ingest.OpenMeteoWeather(down))
    report = ingest.sync(MemoryStore(), live, town, pd.Timestamp("2026-10-08 06:30"))
    assert report["source"] == "none+open-meteo" and report["aq"] == {"new_rows": 0, "error": None} and "HTTPError" in report["wx"]["error"]


def test_rows_for_the_store_keep_gaps_as_null_and_drop_empty_hours():
    frame = pd.DataFrame({"pm25": [80.5, float("nan"), float("nan")], "pm10": [120.0, 130.0, float("nan")]},
                         index=pd.to_datetime(["2026-10-08 05:00", "2026-10-08 06:00", "2026-10-08 07:00"]))
    rows = ingest.rows_of(frame)
    assert rows == {"2026-10-08T05:00": {"pm25": 80.5, "pm10": 120.0}, "2026-10-08T06:00": {"pm25": None, "pm10": 130.0}}
    json.dumps(rows, allow_nan=False)


# ---------------------------------------------------------------- Telegram

class FakeTelegram:
    """Stands in for the Telegram API: records what was sent, and can refuse some chats."""
    def __init__(self, blocked=(), broken=()):
        self.sent, self.blocked, self.broken = [], set(blocked), set(broken)

    def __call__(self, url, payload, timeout=10):
        assert url == "https://api.telegram.org/botTOKEN/sendMessage"
        chat = str(payload["chat_id"])
        if chat in self.broken:
            raise ConnectionError("network down")
        if chat in self.blocked:
            return 403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}
        self.sent.append((chat, payload["text"]))
        return 200, {"ok": True}


def towns_store():
    store = MemoryStore()
    store.put(TOWN, "all", "asansol", {"town_id": "asansol", "name": "Asansol", "has_routes": True})
    store.put(TOWN, "all", "angul", {"town_id": "angul", "name": "Angul", "has_routes": False})
    return store


def say(text, chat=42):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def test_start_subscribes_and_stop_leaves():
    store, api = towns_store(), FakeTelegram()
    bot = telegram.Bot("TOKEN", api)
    reply = telegram.handle_update(store, bot, say("/start asansol"), now_iso="2026-10-08T06:00")
    assert "Asansol" in reply and "/stop" in reply and api.sent[-1] == ("42", reply)
    assert store.query(SUB, "asansol") == [("42", {"chat_id": 42, "joined_at": "2026-10-08T06:00"})]
    telegram.handle_update(store, bot, say("/start asansol"))            # joining twice is still one subscriber
    assert len(store.query(SUB, "asansol")) == 1
    telegram.handle_update(store, bot, say("/start", chat=77))            # one town has alerts, so no name is needed
    assert [c for c, _ in store.query(SUB, "asansol")] == ["42", "77"]
    assert "no more messages" in telegram.handle_update(store, bot, say("/stop"))
    assert [c for c, _ in store.query(SUB, "asansol")] == ["77"]


def test_start_for_a_forecast_only_or_unknown_town_does_not_subscribe():
    store, api = towns_store(), FakeTelegram()
    bot = telegram.Bot("TOKEN", api)
    assert "no route alerts yet" in telegram.handle_update(store, bot, say("/start angul"))
    assert "asansol" in telegram.handle_update(store, bot, say("/start mumbai"))
    assert "/start <town>" in telegram.handle_update(store, bot, say("hello"))
    assert "Asansol" in telegram.handle_update(store, bot, say("/START@saanssetu_bot  ASANSOL "))   # group-style command, any case
    assert [c for c, _ in store.query(SUB, "asansol")] == ["42"] and store.query(SUB, "angul") == []
    assert telegram.handle_update(store, bot, {"update_id": 2, "edited_channel_post": {}}) is None      # not a message: ignored
    assert telegram.handle_update(store, bot, {"message": {"chat": {"id": 1}, "sticker": {}}}) is None


RUN = {"mode": "live", "clock": {"now": "2026-12-07T06:30+05:30"},
       "alert": {"message": {"en": "Plant Gate Rd will likely turn Very Poor around 7:45. Take Station Rd, and leave by 7:00.",
                             "hi": "प्लांट गेट रोड पर लगभग 7:45 बजे हवा बहुत खराब होने की संभावना है।"}}}


def test_alert_goes_to_every_subscriber_and_blocked_ones_are_dropped():
    store, api = towns_store(), FakeTelegram(blocked={"2"}, broken={"3"})
    for chat in ["1", "2", "3", "4"]:
        store.put(SUB, "asansol", chat, {"chat_id": chat})
    store.put(SUB, "angul", "9", {"chat_id": "9"})
    delivered = telegram.notify(store, telegram.Bot("TOKEN", api), {"town_id": "asansol", "name": "Asansol"}, RUN)
    assert delivered == 2 and [c for c, _ in api.sent] == ["1", "4"]
    assert [c for c, _ in store.query(SUB, "asansol")] == ["1", "3", "4"], "blocked is removed; a network failure is kept"
    text = api.sent[0][1]
    assert text.startswith("SaansSetu · Asansol · 6:30\n") and RUN["alert"]["message"]["hi"] in text and RUN["alert"]["message"]["en"] in text
    assert "not a measurement" in text and text.index(RUN["alert"]["message"]["hi"]) < text.index(RUN["alert"]["message"]["en"])
    assert "(replay)" in telegram.alert_text({"name": "Asansol"}, {**RUN, "mode": "replay"})


# ---------------------------------------------------------------- scoring rules

TOWN_PLAIN = prepare({"town_id": "t", "name": "T", "station": {"station_id": "s1"}})
CFG = {"labels": ["poor"], "label_thresholds": {"poor": 91.0}, "suggested_label": "poor", "bad_case": "p95",
       "test": ["2024-10-01", "2025-02-28"], "verdict_level": "SHIP"}


def morning(flag, hours):
    return {"run_id": "t-x", "clock": {"now": "2026-12-07T06:00+05:30"}, "station": {"latest": {"pm25": 80.0}},
            "forecast": {"issued_at": "2026-12-07T06:00+05:30",
                         "hours": [{"time": f"2026-12-07T{h:02d}:00+05:30", "p50": p50, "p95": p95} for h, p50, p95 in hours]},
            "alert": {"decision": "none", "reason": "", "message": None, "school_flag": {"outdoor_pt": flag}}}


@pytest.mark.parametrize("flag,actuals,outcome", [
    ("move_indoors", [100, 95, 70], "hit"), ("ok", [100, 95, 70], "miss"),
    ("move_indoors", [60, 70, 80], "false_alarm"), ("ok", [60, 70, 90.9], "correct_quiet"),
    ("ok", [60, 70, 91.0], "miss")])            # exactly on the threshold counts as bad
def test_outcomes(flag, actuals, outcome):
    store = MemoryStore()
    store.put(MORNING, "t", "2026-12-07", morning(flag, [(7, 90, 110), (8, 95, 120), (9, 85, 100), (10, 70, 90)]))
    store.put_many(AQ, "s1", {f"2026-12-07T{h:02d}:00": {"pm25": v} for h, v in zip([7, 8, 9], actuals)})
    rec = check.score_day(store, TOWN_PLAIN, CFG, "2026-12-07")
    assert rec["outcome"] == outcome and rec["flagged"] == (flag == "move_indoors") and rec["was_bad"] == (max(actuals) >= 91)
    assert [h["time"] for h in rec["hours"]] == ["07:00", "08:00", "09:00"], "10:00 is not a school hour"
    assert rec["mae_model"] == round(sum(abs(p - a) for p, a in zip([90, 95, 85], actuals)) / 3, 1)
    assert rec["mae_persistence"] == round(sum(abs(80 - a) for a in actuals) / 3, 1)
    assert [h["within_p95"] for h in rec["hours"]] == [a <= hi for a, hi in zip(actuals, [110, 120, 100])]


def test_scoring_with_gaps_no_forecast_and_no_readings():
    store = MemoryStore()
    assert check.score_day(store, TOWN_PLAIN, CFG, "2026-12-07")["outcome"] == "no_forecast"
    store.put(MORNING, "t", "2026-12-07", morning("move_indoors", [(7, 90, 110), (8, 95, 120), (9, 85, 100)]))
    rec = check.score_day(store, TOWN_PLAIN, CFG, "2026-12-07")
    assert rec["outcome"] == "no_readings" and rec["mae_model"] is None and rec["hours_scored"] == 0 and len(rec["hours"]) == 3
    store.put_many(AQ, "s1", {"2026-12-07T08:00": {"pm25": 99.0}, "2026-12-07T09:00": {"pm25": None}, "2026-12-07T07:00": {"pm25": 5000}})
    rec = check.score_day(store, TOWN_PLAIN, CFG, "2026-12-07")
    assert rec["hours_scored"] == 1 and rec["outcome"] == "hit" and rec["mae_model"] == 4.0
    assert [h["actual"] for h in rec["hours"]] == [None, 99.0, None], "a missing reading and an impossible one are both gaps"
    assert rec["alert_sent"] is False
    store.put(ALERT, "t", "2026-12-07", {"message": {}})
    assert check.score_day(store, TOWN_PLAIN, CFG, "2026-12-07")["alert_sent"] is True


def test_running_score_pools_hours_over_the_window():
    store = MemoryStore()
    for day, flag, actuals in [("2026-12-01", "ok", [50, 50, 50]), ("2026-12-06", "move_indoors", [100, 100, 100]),
                               ("2026-12-07", "move_indoors", [60, 60, 60])]:
        m = morning(flag, [(7, 90, 110), (8, 90, 110), (9, 90, 110)])
        for h in m["forecast"]["hours"]:
            h["time"] = day + h["time"][10:]
        store.put(MORNING, "t", day, m)
        store.put_many(AQ, "s1", {f"{day}T{h:02d}:00": {"pm25": v} for h, v in zip([7, 8, 9], actuals)})
        check.update(store, TOWN_PLAIN, CFG, None, day, "0.3")
    acc = store.get("accuracy", "t", "current")
    r = acc["running"]
    assert acc["yesterday"]["date"] == "2026-12-07" and [d["date"] for d in acc["daily"]] == ["2026-12-01", "2026-12-06", "2026-12-07"]
    assert (r["hits"], r["misses"], r["false_alarms"], r["correct_quiet"]) == (1, 0, 1, 1)
    assert r["hours_scored"] == 9 and r["mae_model"] == round((40 * 3 + 10 * 3 + 30 * 3) / 9, 1)
    assert r["mae_persistence"] == round((30 * 3 + 20 * 3 + 20 * 3) / 9, 1) and r["p95_coverage"] == 1.0
    assert r["skill_vs_persistence"] == round(1 - r["mae_model"] / r["mae_persistence"], 2) and acc["backtest"] is None
    check.update(store, TOWN_PLAIN, CFG, None, "2026-12-20", "0.3")
    assert [d["date"] for d in store.get("accuracy", "t", "current")["daily"]] == ["2026-12-07", "2026-12-20"], "older than two weeks drops out"


def test_chance_verdict_follows_the_training_rule():
    m = lambda skill, low: {"now": {check.CASE: {"chance": {"poor": {"skill_vs_usual_rate": skill, "skill_vs_usual_rate_ci95": [low, 0.9]}}}}}
    assert check.chance_verdict(m(0.3, 0.1), "poor") == "USE" and check.chance_verdict(m(0.3, -0.1), "poor") == "UNPROVEN"
    assert check.chance_verdict(m(-0.2, -0.4), "poor") == "DO NOT USE" and check.chance_verdict(m(0.3, 0.1), "very_poor") == "NO VERDICT"
    assert check.chance_verdict(None, "poor") == "NO VERDICT"


def test_verdicts_are_for_the_alert_a_family_can_act_on():
    """train.py scores two cases. Stamps mark the start of an hour, so the 6 AM alert is the one it calls lagged_5am:
    the reading for 5 to 6 AM. The 'fresh' case needs the 6 to 7 AM reading, which arrives at 7."""
    assert check.CASE == "lagged_5am"
    level = lambda low: {"skill_vs_persistence": 0.2, "skill_vs_persistence_ci95": [low, 0.3], "mae_model": 10.0, "mae_persistence": 12.5, "n": 300,
                         "p95_coverage": 0.9, "chance": {}}
    metrics = {"now": {"fresh_6am": level(0.15), "lagged_5am": level(-0.02)}}
    cfg = {"verdict_level": "SHIP", "test": ["2024-10-01", "2025-02-28"], "bad_case": "p95"}
    assert check.level_verdict(metrics, cfg) == "DO NOT SHIP", "train.py's stored verdict was for the other case"
    assert check.level_verdict({"now": {"lagged_5am": level(0.05)}}, cfg) == "SHIP"
    assert check.level_verdict(None, cfg) == "SHIP" and check.level_verdict({}, {}) == "NO VERDICT"   # no metrics: fall back
    block = check.backtest_block(cfg, metrics, None)
    assert block["case"] == "lagged_5am" and block["level"]["verdict"] == "DO NOT SHIP" and block["level"]["ci95"] == [-0.02, 0.3]
    assert "5 to 6 AM" in block["note"]
    # the town switcher shows the same verdict, not the one train.py stored
    from core import runner
    run = {"station": None, "alert": {"school_flag": {"outdoor_pt": "unknown"}}, "status": "ok", "mode": "live", "clock": {"now": "t"}}
    line = runner.town_line(TOWN_PLAIN, run, {**cfg, "labels": [], "label_thresholds": {}}, metrics, False)
    assert line["model"]["level"] == "DO NOT SHIP"
