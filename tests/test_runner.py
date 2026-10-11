"""The live path end to end: feed -> store -> run -> alert -> nightly check, for several towns on a fake clock."""
import copy
import json

import pandas as pd
import pytest

import train
from core import ingest, pipeline, runner
from core.store import ALERT, AQ, CHECK, CONFIG, LATEST, MORNING, REPLAY, RUN, SUB, TOWN, WX, MemoryStore
from conftest import DATA


def at(frames, hhmm):
    return next(r for r in frames if r["clock"]["now"][11:16] == hhmm)


# ---------------------------------------------------------------- readings in, history out

def test_archive_source_hands_over_a_reading_only_after_its_hour_has_ended(world):
    station = world.hero["station"]
    now = pd.Timestamp(f"{world.alert_day} 06:30")
    for lag, newest in [(0, "05:00"), (29, "05:00"), (30, "05:00"), (31, "04:00"), (90, "04:00"), (120, "03:00")]:
        got = ingest.ArchiveSource(DATA, lag_minutes=lag).pollutants(station, now - pd.Timedelta(days=3), now)
        assert len(got) > 24 and got.index.min() > now - pd.Timedelta(days=3)
        assert got.index.max() + pd.Timedelta(minutes=60 + lag) <= now, f"lag {lag}: a reading was handed over before its hour ended"
        assert got.index.max() <= pd.Timestamp(f"{world.alert_day} {newest}")
    full = ingest.ArchiveSource(DATA).pollutants(station, now - pd.Timedelta(hours=3), now)
    assert list(full.index.strftime("%H:%M")) == ["04:00", "05:00"], "everything finished is handed over, nothing held back"
    wx = ingest.ArchiveSource(DATA).weather(station, now - pd.Timedelta(days=1), now)
    assert wx.index.max() <= now and "wind_direction_10m" in wx


def test_history_from_the_store_equals_what_training_read(world):
    """Same cleaning, same hours, same values: the model sees at serving time what it saw in training."""
    store, now = MemoryStore(), pd.Timestamp(f"{world.alert_day} 06:30")
    ingest.sync(store, ingest.ArchiveSource(DATA), world.hero, now)
    mine = ingest.history(store, world.hero, now)
    newest = now.floor("h") - pd.Timedelta(hours=1)          # at 06:30 the newest finished hour is the one stamped 05:00
    assert mine[ingest.POLLUTANTS].loc[newest + pd.Timedelta(hours=1):].isna().all().all(), "the 06:00 hour is not over, so it is not there"
    assert pd.notna(mine["pm25"][newest])
    theirs = world.history.reindex(mine.index)
    theirs.loc[theirs.index > newest, [c for c in ingest.POLLUTANTS if c in theirs]] = float("nan")
    for col in theirs.columns:
        # Values and hours must match. Storage type need not: a column the archive holds as whole numbers
        # (humidity, cloud cover) reads back as int64 there, while anything from the store is float64,
        # because it travels through JSON and has to be able to carry a missing hour.
        pd.testing.assert_series_equal(mine[col], theirs[col], check_names=False, check_freq=False,
                                       check_dtype=False, obj=col)
    assert all(t.kind == "f" for t in mine.dtypes), "every column must be float, so a gap can be held as NaN"
    assert mine.index.max() == now.floor("h") and len(mine) == ingest.KEEP_HOURS + 1
    assert set(ingest.POLLUTANTS + ingest.WX_COLS) <= set(mine.columns)   # every column exists even if a feed lacks it


def test_history_is_cleaned_the_way_training_data_was():
    """A stuck sensor and impossible values become gaps, exactly as train.clean does to the training files."""
    from core.townconfig import prepare
    town = prepare({"town_id": "t", "name": "T", "station": {"station_id": "s1"}})
    store, now = MemoryStore(), pd.Timestamp("2026-12-07 06:30")
    hours = pd.date_range("2026-12-06 00:00", "2026-12-07 06:00", freq="h")
    values = [50.0 + i for i in range(len(hours))]
    values[3], values[5] = -4.0, 2600.0                 # impossible
    values[10:18] = [77.0] * 8                           # stuck for eight hours
    store.put_many(AQ, "s1", {t.strftime(ingest.HOUR): {"pm25": v, "pm10": 100.0 + i} for i, (t, v) in enumerate(zip(hours, values))})
    store.put_many(WX, "s1", {hours[-1].strftime(ingest.HOUR): {"wind_speed_10m": 2.0, "wind_direction_10m": 90.0}})
    h = ingest.history(store, town, now)
    assert h.index[-1] == pd.Timestamp("2026-12-07 06:00") and h.index.freqstr.lower() == "h"
    pm = h["pm25"].loc[hours[0]:]
    assert pm.isna().sum() == 10 and pm[hours[3]] != pm[hours[3]] and pm[hours[5]] != pm[hours[5]] and pm.loc[hours[10]:hours[17]].isna().all()
    assert pm[hours[9]] == 59.0 and pm[hours[18]] == 68.0 and h["pm10"][hours[12]] == 112.0, "only the stuck column is blanked"
    assert h["wind_direction_10m"].iloc[-1] == 90.0 and h["boundary_layer_height"].isna().all() and h["no2"].isna().all()
    assert h["pm25"].loc[:hours[0] - pd.Timedelta(hours=1)].isna().all()


def test_sync_reads_everything_once_then_only_the_newest_hours(world):
    asked = []

    class Counting(ingest.ArchiveSource):
        def pollutants(self, station, since, until):
            asked.append((pd.Timestamp(until) - pd.Timestamp(since)) / pd.Timedelta(hours=1))
            return super().pollutants(station, since, until)

    store, now = MemoryStore(), pd.Timestamp(f"{world.alert_day} 06:00")
    first = ingest.sync(store, Counting(DATA), world.hero, now)
    second = ingest.sync(store, Counting(DATA), world.hero, now + pd.Timedelta(minutes=15))
    third = ingest.sync(store, Counting(DATA), world.hero, now + pd.Timedelta(hours=1))
    # after the first read it asks only for the overlap plus the time since the newest finished hour (an hour or two)
    assert asked[0] >= ingest.KEEP_HOURS and asked[1] < ingest.OVERLAP_HOURS + 2 and asked[2] < ingest.OVERLAP_HOURS + 3
    assert first["aq"]["new_rows"] > 200 and second["aq"]["new_rows"] <= ingest.OVERLAP_HOURS + 1 and third["aq"]["error"] is None
    hours = [h for h, _ in store.query(AQ, world.hero["station"]["station_id"])]
    assert hours == sorted(set(hours)) and hours[-1] == now.strftime(ingest.HOUR), "at 07:00 the 06:00 reading has just arrived"
    assert store.ttl[(AQ, world.hero["station"]["station_id"], hours[-1])] == 20


def test_store_path_gives_the_same_run_as_the_direct_pipeline(world, replayed):
    """What the Lambda does (feed -> store -> run) must equal calling run_once on the training frame."""
    _, frames = replayed
    sid = world.hero["station"]["station_id"]
    for hhmm in ["05:15", "06:00", "06:30", "07:15", "08:30"]:
        via_store = at(frames[world.hero["town_id"]], hhmm)
        direct = pipeline.run_once(copy.deepcopy(world.hero), world.history, world.cfg, world.root / sid / "models",
                                   pd.Timestamp(f"{world.alert_day} {hhmm}"), mode="replay", replay_of=world.alert_day,
                                   already_sent_today=hhmm > "06:00")
        for key in ["station", "weather", "forecast", "slots", "routes", "departures", "status"]:
            assert via_store[key] == direct[key], (hhmm, key)
        assert via_store["alert"]["decision"] == direct["alert"]["decision"], hhmm


# ---------------------------------------------------------------- the morning, tick by tick

def test_exactly_one_alert_goes_out_and_not_before_the_alert_hour(world):
    sent = []
    towns = world.copy_towns()
    store = MemoryStore()
    for chat in ["111", "222"]:
        store.put(SUB, world.hero["town_id"], chat, {"chat_id": chat})
    store, frames = runner.replay_day(towns, DATA, world.root, world.alert_day, store=store,
                                      notify=lambda town, run: sent.append((town["town_id"], run["clock"]["now"][11:16])) or 2)
    hero = frames[world.hero["town_id"]]
    sends = [r for r in hero if r["alert"]["decision"] == "send"]
    assert len(sends) == 1 and sent == [(world.hero["town_id"], sends[0]["clock"]["now"][11:16])]
    first = sends[0]
    assert first["clock"]["now"][11:16] >= world.hero["alert_rule"]["earliest"] and first["alert"]["recipients"] == 2
    before = [r for r in hero if r["clock"]["now"] < first["clock"]["now"]]
    assert before and all(r["alert"]["message"] is None and "earlier_today" not in r["alert"] for r in before)
    after = [r for r in hero if r["clock"]["now"] > first["clock"]["now"]]
    assert after and all(r["alert"]["decision"] != "send" for r in after)
    assert all(r["alert"]["earlier_today"]["message"] == first["alert"]["message"] for r in after), "the dashboard keeps the sent message"
    assert any(r["alert"]["reason_code"] == "already_sent_today" for r in after)
    stored = store.get(ALERT, world.hero["town_id"], world.alert_day)
    assert stored["message"] == first["alert"]["message"] and stored["recipients"] == 2
    for town in world.others:      # forecast-only towns never message anyone
        assert all(r["alert"]["decision"] != "send" for r in frames[town["town_id"]])
        assert store.get(ALERT, town["town_id"], world.alert_day) is None


def test_two_overlapping_runs_send_only_once(world):
    """Another run claims the day between this run's check and its send. This run must back off."""
    class Racing(MemoryStore):
        def get(self, kind, key, sort):
            if kind == ALERT and not getattr(self, "peeked", False):
                self.peeked = True
                return None             # this run looks: nothing sent yet
            return super().get(kind, key, sort)

    store, sent = Racing(), []
    now = pd.Timestamp(f"{world.alert_day} 06:30")
    winner = {"decision": "send", "message": {"en": "from the other run", "hi": "x"}, "sent_at": "t", "recipients": 5, "reason": "r"}
    store.put(ALERT, world.hero["town_id"], world.alert_day, winner)   # ...but the other run has already claimed it
    run = runner.run_town(store, copy.deepcopy(world.hero), world.root, ingest.ArchiveSource(DATA), now, mode="replay",
                          model_set="models", notify=lambda town, run: sent.append(1) or 1)
    assert sent == [] and run["alert"]["decision"] == "none" and run["alert"]["reason_code"] == "already_sent_today"
    assert run["alert"]["message"] is None and run["steps"][-1]["status"] == "not_needed"
    assert run["alert"]["earlier_today"]["message"]["en"] == "from the other run"
    assert store.get(ALERT, world.hero["town_id"], world.alert_day) == winner


def test_a_telegram_failure_does_not_lose_the_run(world):
    def broken(town, run):
        raise ConnectionError("telegram unreachable")
    store, now = MemoryStore(), pd.Timestamp(f"{world.alert_day} 06:30")
    run = runner.run_town(store, copy.deepcopy(world.hero), world.root, ingest.ArchiveSource(DATA), now, mode="replay",
                          model_set="models", notify=broken)
    assert run["status"] == "ok" and run["alert"]["decision"] == "send" and run["alert"]["recipients"] == 0
    assert "telegram unreachable" in run["alert"]["delivery_problem"]
    assert store.get(LATEST, world.hero["town_id"], "run")["run_id"] == run["run_id"]


def test_what_one_tick_leaves_in_the_store(world, replayed):
    store, frames = replayed
    for town_id, town in world.towns.items():
        runs = frames[town_id]
        assert [r["clock"]["now"][11:16] for r in runs][:3] == ["05:00", "05:15", "05:30"] and len(runs) == 17
        assert store.get(LATEST, town_id, "run") == runs[-1]
        assert len(store.query(RUN, town_id)) == len(store.query(REPLAY, town_id)) == 17
        assert store.ttl[(RUN, town_id, f"{world.alert_day}T06:30")] == 7
        line = store.get(TOWN, "all", town_id)
        assert line["has_routes"] == town["has_routes"] and line["model"]["level"] in ("SHIP", "DO NOT SHIP", "NO VERDICT")
        assert line["now"] == runs[-1]["station"]["latest"] if runs[-1]["station"] else True
        assert (line["alert_today"] is None) == (not town["has_routes"])
        cfg = store.get(CONFIG, town_id, "current")
        assert cfg["town_id"] == town_id and cfg["has_routes"] == town["has_routes"]
        for r in runs:
            assert r["mode"] == "replay" and r["clock"]["replay_of"] == world.alert_day and r["feed"]["source"] == "archive"
            assert len(json.dumps(r, ensure_ascii=False).encode()) < 350_000, "must fit one DynamoDB item"
    assert store.get(TOWN, "all", world.hero["town_id"])["alert_today"] is True


def test_the_days_call_is_the_first_full_run_at_the_alert_hour(world, replayed):
    store, frames = replayed
    for town_id, town in world.towns.items():
        morning = store.get(MORNING, town_id, world.alert_day)
        ok = [r for r in frames[town_id] if r["clock"]["now"][11:16] >= town["alert_rule"]["earliest"]
              and r["alert"]["school_flag"]["outdoor_pt"] != "unknown"]
        if not ok:
            assert morning is None
            continue
        assert morning["run_id"] == ok[0]["run_id"]
        later = frames[town_id][-1]["morning_call"]
        assert later["run_id"] == morning["run_id"] and later["school_flag"] == morning["alert"]["school_flag"]
        early = frames[town_id][0]
        assert early["morning_call"] is None   # 05:00: the call has not been made yet


def test_park_leaves_latest_showing_the_chosen_moment(world):
    store, frames = runner.replay_day({world.hero["town_id"]: copy.deepcopy(world.hero)}, DATA, world.root, world.alert_day,
                                      start="06:00", end="07:00", park_at="06:30")
    town_id = world.hero["town_id"]
    assert store.get(LATEST, town_id, "run") == at(frames[town_id], "06:30")
    assert store.get(TOWN, "all", town_id)["updated_at"][11:16] == "06:30"


# ---------------------------------------------------------------- when things go wrong

def test_one_town_failing_does_not_stop_the_others(world):
    towns = world.copy_towns()
    towns["ghost"] = {**copy.deepcopy(world.hero), "town_id": "ghost", "name": "Ghost",
                      "station": {"station_id": "site_none", "name": "No such station", "lat": 0, "lon": 0}}
    store = MemoryStore()
    out = runner.run_all(store, towns, world.root, ingest.ArchiveSource(DATA), pd.Timestamp(f"{world.alert_day} 06:30"),
                         mode="replay", model_set="models")
    assert out["ghost"]["status"] == "failed" and out[world.hero["town_id"]]["status"] == "ok"
    assert out[world.hero["town_id"]]["alert"] == "send"
    ghost = store.get(LATEST, "ghost", "run")
    assert ghost["status"] == "failed" and "site_none" in ghost["steps"][-1]["detail"] and ghost["status_message"]["hi"]
    assert store.get(TOWN, "all", "ghost")["status"] == "failed" and store.get(TOWN, "all", "ghost")["now"] is None
    assert store.get(MORNING, "ghost", world.alert_day) is None


def test_a_store_that_breaks_for_one_town_is_reported_not_raised(world):
    class Flaky(MemoryStore):
        def put(self, kind, key, sort, doc, ttl_days=None):
            if key == "ghost":
                raise RuntimeError("throttled")
            super().put(kind, key, sort, doc, ttl_days)

    towns = {"ghost": {**copy.deepcopy(world.hero), "town_id": "ghost"}, world.hero["town_id"]: copy.deepcopy(world.hero)}
    out = runner.run_all(Flaky(), towns, world.root, ingest.ArchiveSource(DATA), pd.Timestamp(f"{world.alert_day} 06:30"),
                         mode="replay", model_set="models")
    assert out["ghost"]["status"] == "failed" and "throttled" in out["ghost"]["reason"]
    assert out[world.hero["town_id"]]["status"] == "ok"


def test_feed_going_down_mid_morning_degrades_and_holds(world):
    class Dies(ingest.ArchiveSource):
        def pollutants(self, station, since, until):
            if pd.Timestamp(until) >= pd.Timestamp(f"{world.alert_day} 05:00"):
                raise TimeoutError("feed timed out")
            return super().pollutants(station, since, until)

    store, town, source = MemoryStore(), copy.deepcopy(world.hero), Dies(DATA)
    go = lambda hhmm: runner.run_town(store, town, world.root, source, pd.Timestamp(f"{world.alert_day} {hhmm}"), mode="replay", model_set="models")
    assert go("04:30")["status"] == "ok"
    still = go("05:30")          # the feed is down, but the reading for 03:00 to 04:00 is still fresh enough
    assert still["status"] == "ok" and "feed timed out" in still["feed"]["aq"]["error"] and "feed problem" in still["steps"][0]["detail"]
    down = go("06:30")
    assert down["status"] == "degraded" and down["station"]["is_stale"] and down["alert"]["decision"] == "held"
    assert down["alert"]["reason_code"] == "stale_data" and down["status_message"]["en"].startswith("Station feed is")
    assert store.get(ALERT, town["town_id"], world.alert_day) is None and store.get(MORNING, town["town_id"], world.alert_day) is None


def test_a_feed_with_nothing_at_all_gives_a_failed_run_not_a_crash(world):
    class Empty(ingest.ArchiveSource):
        def pollutants(self, station, since, until):
            raise FileNotFoundError("no file")

    run = runner.run_town(MemoryStore(), copy.deepcopy(world.hero), world.root, Empty(DATA),
                          pd.Timestamp(f"{world.alert_day} 06:30"), mode="replay", model_set="models")
    assert run["status"] == "failed" and run["forecast"] is None and "feed problem" in run["steps"][0]["detail"]


def test_weather_feed_down_still_forecasts_without_the_plume(world):
    class NoWeather(ingest.ArchiveSource):
        def weather(self, station, since, until):
            raise TimeoutError("weather timed out")

    run = runner.run_town(MemoryStore(), copy.deepcopy(world.hero), world.root, NoWeather(DATA),
                          pd.Timestamp(f"{world.alert_day} 06:30"), mode="replay", model_set="models")
    assert run["status"] == "ok" and len(run["forecast"]["hours"]) == 4 and run["weather"]["now"]["wind_from_deg"] is None
    assert run["steps"][2]["status"] == "partial" and "plume not applied" in run["steps"][2]["detail"]
    assert "weather timed out" in run["feed"]["wx"]["error"]


def test_a_feed_publishing_an_hour_late_still_reaches_the_school_window(world):
    """The 04:00 to 05:00 reading, published an hour after it ended, is what a 06:15 run has. It is 75 minutes old."""
    store, frames = runner.replay_day({world.hero["town_id"]: copy.deepcopy(world.hero)}, DATA, world.root, world.alert_day,
                                      start="06:00", end="06:30", lag_minutes=60)
    run = at(frames[world.hero["town_id"]], "06:15")
    assert run["station"]["latest"]["time"][11:16] == "04:00" and run["station"]["age_minutes"] == 75
    assert run["status"] == "ok" and run["departures"] and [h["horizon_h"] for h in run["forecast"]["hours"]][-1] == 4


# ---------------------------------------------------------------- the nightly check

def test_the_replayed_morning_is_scored_against_what_the_station_recorded(world, replayed):
    store, frames = replayed
    town_id, sid = world.hero["town_id"], world.hero["station"]["station_id"]
    rec = store.get(CHECK, town_id, world.alert_day)
    morning = store.get(MORNING, town_id, world.alert_day)
    high = world.cfg["bad_case"]
    assert rec["issued_at"] == morning["forecast"]["issued_at"] and [h["time"] for h in rec["hours"]] == ["07:00", "08:00", "09:00"]
    raw = train._read(DATA / f"aq_{sid}.csv.gz")["pm25"]
    raw = raw.groupby(raw.index.floor("h")).mean()
    for h in rec["hours"]:
        actual = round(float(raw[pd.Timestamp(f"{world.alert_day} {h['time']}")]), 1)
        said = next(x for x in morning["forecast"]["hours"] if x["time"][11:16] == h["time"])
        assert h["actual"] == actual and h["forecast_p50"] == said["p50"] and h[f"forecast_{high}"] == said[high]
        assert h["error"] == round(abs(said["p50"] - actual), 1) and h[f"within_{high}"] == (actual <= said[high])
        assert h["persistence"] == morning["station"]["latest"]["pm25"]
    assert rec["mae_model"] == round(sum(h["error"] for h in rec["hours"]) / 3, 1)
    assert rec["alert_sent"] is True and rec["flagged"] is True and rec["label"] == (world.hero.get("alert_label") or world.cfg["suggested_label"])
    assert rec["outcome"] == ("hit" if rec["was_bad"] else "false_alarm")
    acc = store.get("accuracy", town_id, "current")
    assert acc["yesterday"] == rec and [d["date"] for d in acc["daily"]] == [world.alert_day], "the day before was not running, so it is not scored"
    assert acc["running"]["mornings_scored"] == 1 and acc["running"]["hours_scored"] == 3 and acc["running"]["mae_model"] == rec["mae_model"]
    assert acc["backtest"]["level"]["verdict"] == world.cfg["verdict_level"] and acc["backtest"]["chance"]["label"] == rec["label"]
    assert len(acc["backtest"]["chance"]["reliability"]) >= 3


def test_yesterday_is_scored_on_the_first_tick_of_the_next_day(world):
    store, town, source = MemoryStore(), copy.deepcopy(world.hero), ingest.ArchiveSource(DATA)
    day = pd.Timestamp(world.alert_day)
    go = lambda t: runner.run_town(store, town, world.root, source, t, mode="replay", model_set="models")
    go(day + pd.Timedelta(hours=6, minutes=30))
    assert store.get(CHECK, town["town_id"], world.alert_day) is None, "not scored on the same day"
    assert store.get(CHECK, town["town_id"], str((day - pd.Timedelta(days=1)).date())) is None
    go(day + pd.Timedelta(days=1, minutes=15))
    rec = store.get(CHECK, town["town_id"], world.alert_day)
    assert rec["hours_scored"] == 3 and rec["outcome"] in ("hit", "false_alarm")
    rec["marker"] = True
    store.put(CHECK, town["town_id"], world.alert_day, rec)
    go(day + pd.Timedelta(days=1, minutes=30))
    assert store.get(CHECK, town["town_id"], world.alert_day).get("marker"), "scored once, not on every tick"


def test_a_quiet_morning_sends_nothing_and_scores_as_quiet(world):
    if world.quiet_day is None:
        pytest.skip("no quiet morning found in the held-out winter")
    store, frames = runner.replay_day({world.hero["town_id"]: copy.deepcopy(world.hero)}, DATA, world.root, world.quiet_day,
                                      start="06:00", end="06:30")
    runs = frames[world.hero["town_id"]]
    assert len(runs) == 3 and all(r["alert"]["decision"] == "none" and r["alert"]["message"] is None for r in runs)
    assert store.get(ALERT, world.hero["town_id"], world.quiet_day) is None
    rec = store.get(CHECK, world.hero["town_id"], world.quiet_day)
    assert rec["alert_sent"] is False and rec["flagged"] is False and rec["outcome"] in ("correct_quiet", "miss")
