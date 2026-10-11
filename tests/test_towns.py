"""Multi-town: town files with and without routes, and what one run gives a forecast-only town."""
import copy
import json

import pandas as pd
import pytest

from core import pipeline
from core.townconfig import ConfigError, load_town, load_towns, prepare, public_config
from conftest import DATA, FIX

CFG = {"labels": ["moderate", "poor"], "label_thresholds": {"moderate": 61.0, "poor": 91.0, "very_poor": 121.0},
       "suggested_label": "moderate", "bad_case": "p95"}


def test_a_station_is_enough_for_a_forecast_only_town():
    t = prepare({"town_id": "angul", "name": "Angul", "station": {"station_id": "site_5939", "name": "Hakimapada", "lat": 20.8, "lon": 85.1}})
    assert t["has_routes"] is False and t["_segments"] == {} and t["school"]["starts_at"] == "08:00"
    assert t["school_hours"] == [7, 8, 9] and t["alert_rule"]["earliest"] == "06:00"


def test_half_a_route_setup_is_rejected():
    base = {"town_id": "x", "name": "X", "station": {"station_id": "s"}}
    routed = json.loads((FIX / "towns" / "testville.json").read_text(encoding="utf-8"))
    for breaker in [lambda t: t.pop("school"), lambda t: t.pop("segments_geojson"), lambda t: t.pop("usual_departure")]:
        t = copy.deepcopy(routed)
        breaker(t)
        with pytest.raises(ConfigError):
            prepare(t)
    with pytest.raises(ConfigError):   # segments without routes: someone forgot half the file
        prepare({**base, "segments_geojson": routed["segments_geojson"]})
    with pytest.raises(ConfigError):
        prepare({"town_id": "x", "name": "X", "station": {"name": "no id"}})


def test_load_towns_skips_a_broken_file_and_reports_it(tmp_path):
    for name in ["testville.json", "otherton.json"]:
        (tmp_path / name).write_text((FIX / "towns" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "nostation.json").write_text('{"town_id": "n", "name": "N"}', encoding="utf-8")
    (tmp_path / "twin.json").write_text((FIX / "towns" / "otherton.json").read_text(encoding="utf-8"), encoding="utf-8")
    towns, problems = load_towns(tmp_path)
    assert set(towns) == {"testville", "otherton"}
    assert set(problems) == {"broken.json", "nostation.json", "twin.json"} and "used by two files" in problems["twin.json"]


def test_public_config_for_both_kinds_of_town():
    routed = public_config(load_town(FIX / "towns" / "testville.json"), CFG, "0.3")
    assert routed["has_routes"] and routed["alert_label"] == "moderate" and [b["id"] for b in routed["bands"]][3] == "poor"
    assert [r["route_id"] for r in routed["routes"]] == ["plant-gate", "station-rd"] and routed["routes"][0]["is_usual"]
    assert routed["routes"][0]["segment_ids"][0] == "plant-gate-01" and routed["routes"][0]["walk_minutes"] > 10
    props = routed["segments_geojson"]["features"][0]["properties"]
    assert props["length_m"] > 0 and props["distance_to_plant_m"] > 0 and props["road"] == "main"
    assert routed["chance_labels"] == [{"id": "moderate", "pm25_min": 61.0}, {"id": "poor", "pm25_min": 91.0}]
    plain = public_config(load_town(FIX / "towns" / "otherton.json"), CFG, "0.3")
    assert plain["has_routes"] is False and plain["alert_label"] == "poor" and "routes" not in plain and "plant" not in plain
    json.dumps([routed, plain])
    assert not any(k.startswith("_") for k in routed)   # computed internals stay out


# ---------------------------------------------------------------- one run, forecast-only town

def run_at(world, town, clock, **kw):
    import train
    sid = town["station"]["station_id"]
    cfg = json.loads((world.root / sid / "config.json").read_text())
    history = train.load(DATA, sid)
    return pipeline.run_once(copy.deepcopy(town), history, cfg, world.root / sid / "models", pd.Timestamp(clock), mode="replay", **kw), cfg


def without_routes(town):
    """The same town and station with its routes taken away."""
    return prepare(copy.deepcopy({k: town[k] for k in ("town_id", "name", "station", "alert_label") if k in town}))


def test_forecast_only_town_gets_forecast_and_school_flag_but_no_route_advice(world):
    town = without_routes(world.hero)
    run, cfg = run_at(world, town, f"{world.alert_day} 06:30")
    assert run["status"] == "ok" and run["has_routes"] is False
    assert [s["status"] for s in run["steps"]] == ["ok", "ok", "skipped", "skipped", "not_needed"]
    assert len(run["forecast"]["hours"]) == len(cfg["horizons"])
    assert run["routes"] == [] and run["slots"] == [] and run["departures"] is None
    a = run["alert"]
    assert a["decision"] == "none" and a["reason_code"] == "no_routes" and a["message"] is None and a["recipients"] == 0
    assert a["school_flag"]["outdoor_pt"] == "move_indoors"    # same morning, same station: the flag matches the routed run
    routed, _ = run_at(world, world.hero, f"{world.alert_day} 06:30")
    assert routed["alert"]["school_flag"] == a["school_flag"] and routed["forecast"]["hours"] == run["forecast"]["hours"]
    json.dumps(run)


def test_forecast_only_town_on_a_stale_feed_is_degraded(world):
    import train
    town = without_routes(world.hero)
    sid = town["station"]["station_id"]
    now = pd.Timestamp(f"{world.alert_day} 07:15")
    history = train.load(DATA, sid)
    # The last reading is for 04:00 to 05:00. A forecast from it would still reach school time, but by 07:15 it is too old.
    history.loc[history.index > pd.Timestamp(f"{world.alert_day} 04:00"), "pm25"] = float("nan")
    cfg = json.loads((world.root / sid / "config.json").read_text())
    stale = pipeline.run_once(town, history, cfg, world.root / sid / "models", now, mode="replay")
    assert stale["station"]["latest"]["time"][11:16] == "04:00" and stale["station"]["age_minutes"] == 135
    assert stale["forecast"]["hours"][-1]["time"][11:16] == "08:00", "the forecast does reach school time"
    assert stale["status"] == "degraded" and stale["station"]["is_stale"] and stale["status_message"]["hi"]
    assert stale["alert"]["decision"] == "held" and stale["alert"]["school_flag"]["outdoor_pt"] == "unknown"


# ---------------------------------------------------------------- the clock through a live day (routed town)

def test_before_the_morning_window_there_is_a_forecast_but_no_advice(world):
    night = world.history["pm25"].loc[f"{world.alert_day} 00:00": f"{world.alert_day} 02:00"].dropna()
    if night.empty:
        pytest.skip("no reading between midnight and 2 AM on the test morning")
    run, _ = run_at(world, world.hero, night.index[-1] + pd.Timedelta(minutes=30))
    assert run["status"] == "ok" and run["status_message"] is None, "an early run on fresh data is not a degraded run"
    assert run["alert"]["decision"] == "held" and run["alert"]["reason_code"] == "before_morning_window"
    assert run["departures"] is None and len(run["forecast"]["hours"]) == 4
    assert run["alert"]["school_flag"]["outdoor_pt"] == "unknown"


def test_no_parent_is_messaged_before_the_alert_hour(world):
    early, _ = run_at(world, world.hero, f"{world.alert_day} 05:15")
    assert early["departures"] and early["alert"]["decision"] == "none" and early["alert"]["message"] is None
    on_time, _ = run_at(world, world.hero, f"{world.alert_day} 06:30")
    assert on_time["alert"]["decision"] == "send"
    late_town = copy.deepcopy(world.hero)
    late_town["alert_rule"]["earliest"] = "06:45"          # the same run, with the alert hour moved past it
    held_back, _ = run_at(world, late_town, f"{world.alert_day} 06:30")
    a = held_back["alert"]
    assert a["decision"] == "none" and a["reason_code"] == "too_early" and "6:45" in a["reason"]
    assert a["message"] is None and a["sent_at"] is None and a["recipients"] == 0 and held_back["steps"][-1]["status"] == "not_needed"
    assert a["school_flag"] == on_time["alert"]["school_flag"], "the school flag does not wait for the alert hour"
    assert held_back["departures"] == on_time["departures"]


def test_after_the_window_the_school_flag_is_still_given(world):
    run, _ = run_at(world, world.hero, f"{world.alert_day} 08:15")
    assert run["alert"]["reason_code"] == "after_departure_window" and run["departures"] is None
    assert run["alert"]["school_flag"]["outdoor_pt"] in ("move_indoors", "ok")


def test_failed_run_document_has_the_same_shape(world):
    run = pipeline.failed_run(world.hero, f"{world.alert_day} 06:30", "live", "FileNotFoundError: models missing")
    assert run["status"] == "failed" and run["alert"]["decision"] == "held" and run["steps"][-1]["status"] == "failed"
    for key in ["schema_version", "town_id", "run_id", "mode", "clock", "status_message", "station", "weather", "forecast",
                "steps", "slots", "routes", "departures", "alert"]:
        assert key in run
    json.dumps(run)
