"""End-to-end tests of one run. They need station data and trained models, and skip when those are absent.

By default they look for ./saanssetu_data and the newest ./out*/ folder, using towns/asansol.json.
Point them elsewhere with:
  SAANSSETU_DATA, SAANSSETU_MODELS, SAANSSETU_TOWN
"""
import copy
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import train
from core import forecasting, pipeline
from core.bands import ORDER
from core.geo import minutes
from core.townconfig import load_town, prepare

ROOT = Path(__file__).parent.parent
TOWN = Path(os.environ.get("SAANSSETU_TOWN", ROOT / "towns" / "asansol.json"))
DATA = Path(os.environ.get("SAANSSETU_DATA", ROOT / "saanssetu_data"))
POLL = ["pm25", "pm10", "no2", "co", "so2"]


def _station_dir(sid):
    if os.environ.get("SAANSSETU_MODELS"):
        roots = [Path(os.environ["SAANSSETU_MODELS"])]
    else:
        roots = sorted(ROOT.glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [ROOT / "out"]
    return next((r / sid for r in roots if (r / sid / "config.json").exists()), None)


@pytest.fixture(scope="module")
def ctx():
    town = load_town(TOWN)
    sid = town["station"]["station_id"]
    sdir = _station_dir(sid)
    if sdir is None or not (DATA / f"aq_{sid}.csv.gz").exists():
        pytest.skip(f"no data or models for {sid}; run fetch_data.py and train.py first")
    cfg = json.loads((sdir / "config.json").read_text())
    history = train.load(DATA, sid)
    pm = history["pm25"]
    days = pd.date_range(cfg["test"][0], cfg["test"][1], freq="D")
    # A run at 06:30 can use readings up to the one stamped 05:00, which covers 05:00 to 06:00.
    usable = [d for d in days if pm.loc[d + pd.Timedelta(hours=-170): d + pd.Timedelta(hours=5)].notna().mean() > 0.8
              and pd.notna(pm.get(d + pd.Timedelta(hours=5)))]
    if not usable:
        pytest.skip("no morning in the held-out winter has a reading for 5 to 6 AM")
    worst = max(usable, key=lambda d: pm[d + pd.Timedelta(hours=5)])
    return {"town": town, "cfg": cfg, "history": history, "models": sdir / "models", "final": sdir / "final",
            "now": worst + pd.Timedelta(hours=6, minutes=30), "days": usable}


def go(ctx, now=None, history=None, **kw):
    return pipeline.run_once(copy.deepcopy(ctx["town"]), ctx["history"] if history is None else history, ctx["cfg"], ctx["models"],
                             ctx["now"] if now is None else now, mode="replay", **kw)


def missing_keys(sample, real, path=""):
    """Key paths present in the UI sample but absent from a real run."""
    out = []
    if isinstance(sample, dict) and isinstance(real, dict):
        for k, v in sample.items():
            if k not in real:
                out.append(f"{path}.{k}")
            elif k != "chance":            # which chance labels exist depends on the town
                out += missing_keys(v, real[k], f"{path}.{k}")
    elif isinstance(sample, list) and isinstance(real, list) and sample and real:
        out += missing_keys(sample[0], real[0], f"{path}[]")
    return out


def test_run_matches_the_dashboard_contract(ctx):
    run = go(ctx)
    assert run["status"] == "ok" and run["departures"], f"expected a full run for {ctx['now']}, got {run['steps']}"
    sample = json.loads((Path(__file__).parent / "fixtures" / "run.alert.json").read_text(encoding="utf-8"))
    assert missing_keys(sample, run) == [], "the dashboard sample has fields a real run does not produce"
    assert [s["step"] for s in run["steps"]] == ["ingest", "forecast", "exposure", "departure", "alert"]
    json.dumps(run)   # must be plain JSON: no numpy or pandas types


def test_run_is_internally_consistent(ctx):
    run = go(ctx)
    high = run["forecast"]["bad_case"]
    for h in run["forecast"]["hours"]:
        assert h[high] >= h["p50"] >= 0 and all(0 <= v <= 1 for v in h["chance"].values())
        ordered = sorted(h["chance"], key=ctx["cfg"]["label_thresholds"].get)
        assert all(h["chance"][b] <= h["chance"][a] + 1e-9 for a, b in zip(ordered, ordered[1:]))
    ids = {f["properties"]["segment_id"] for f in ctx["town"]["segments_geojson"]["features"]}
    assert {s["segment_id"] for r in run["routes"] for s in r["segments"]} == ids
    for r in run["routes"]:
        for s in r["segments"]:
            assert len(s["pm25"]) == len(s["band"]) == len(run["slots"])
        assert r["summary"]["peak"]["pm25"] == max(r["summary"]["worst_pm25"])
    d = run["departures"]
    on_time = [o for o in d["options"] if o["arrives_on_time"]]
    assert d["best"] == min(on_time, key=lambda o: (o["dose"], o["leave"]))
    assert d["saving_vs_usual_pct"] == round((1 - d["best"]["dose"] / d["usual"]["dose"]) * 100)
    now_min = ctx["now"].hour * 60 + ctx["now"].minute
    assert all(minutes(o["leave"]) >= now_min for o in d["options"])
    a = run["alert"]
    assert a["decision"] in ("send", "none") and (a["message"] is not None) == (a["decision"] == "send")
    st = run["station"]
    assert len(st["history_24h"]) == 24 and st["history_24h"][-1]["time"] == st["latest"]["time"] and not st["is_stale"]


def test_run_uses_only_readings_whose_hour_has_ended(ctx):
    """A reading stamped 06:00 is the 06:00 to 07:00 average, so at 06:30 it does not exist yet.
    Scramble every reading whose hour is still running or yet to come. The run must not change."""
    h = ctx["history"].copy()
    later = h.index > ctx["now"] - pd.Timedelta(hours=1)
    assert later.sum() > 0 and h.index[later][0] == ctx["now"].floor("h"), "the 06:00 reading is among the scrambled ones"
    rng = np.random.default_rng(0)
    cols = [c for c in POLL if c in h]
    h.loc[later, cols] = rng.random((later.sum(), len(cols))) * 900
    for variant in ["now", "fut"]:
        assert go(ctx, history=h, variant=variant) == go(ctx, variant=variant), variant


def test_serving_forecast_equals_training_forecast(ctx):
    """The cached serving path must give exactly what train.forecast gives on the full frame."""
    reading_at = ctx["now"].floor("h")
    for variant in ["now", "fut"]:
        for folder in [ctx["models"], ctx["final"]]:
            if not (folder / "calibration.json").exists():
                continue
            mine, _ = forecasting.forecast(ctx["history"], ctx["cfg"], folder, reading_at, variant)
            theirs = train.forecast(ctx["history"], ctx["cfg"], folder, reading_at, variant)
            assert {str(k): v for k, v in mine.items()} == theirs, (variant, folder.name)


def test_stale_feed_holds_the_alert(ctx):
    h = ctx["history"].copy()
    cut = ctx["now"].normalize() + pd.Timedelta(hours=3)
    h.loc[h.index > cut, [c for c in POLL if c in h]] = np.nan
    run = go(ctx, history=h)
    assert run["status"] == "degraded" and run["station"]["is_stale"]
    assert run["station"]["latest"]["time"][11:16] == "03:00" and run["station"]["age_minutes"] == 150, "03:00 to 04:00, then 2 h 30 min"
    assert run["alert"]["decision"] == "held" and run["alert"]["reason_code"] == "stale_data"
    assert run["routes"] == [] and run["slots"] == [] and run["departures"] is None and run["status_message"]["hi"]
    assert [s["status"] for s in run["steps"]] == ["stale", "partial", "skipped", "skipped", "held"]


def test_the_clock_decides_which_reading_is_the_latest(ctx):
    """06:59: the newest finished hour started at 05:00. 07:00: the 06:00 hour has just ended and takes over."""
    day = ctx["now"].normalize()
    before = go(ctx, now=day + pd.Timedelta(hours=6, minutes=59))
    at = go(ctx, now=day + pd.Timedelta(hours=7))
    assert before["station"]["latest"]["time"][11:16] == "05:00" and before["station"]["age_minutes"] == 59
    if pd.notna(ctx["history"]["pm25"].get(day + pd.Timedelta(hours=6))):
        assert at["station"]["latest"]["time"][11:16] == "06:00" and at["station"]["age_minutes"] == 0
        assert at["station"]["latest"]["pm25"] == round(float(ctx["history"]["pm25"][day + pd.Timedelta(hours=6)]), 1)
    assert before["station"]["reading_minutes"] == 60 and before["forecast"]["based_on_reading_at"][11:16] == "05:00"
    assert [h["time"][11:16] for h in before["forecast"]["hours"]] == ["06:00", "07:00", "08:00", "09:00"]
    assert "covers 05:00 to 06:00" in before["steps"][0]["detail"]


def test_hourly_values_sit_at_the_middle_of_their_hour():
    """The station curve the routes are built on: a value for 07:00 to 08:00 belongs at 07:30, not 07:00."""
    day = pd.Timestamp("2025-01-14")
    fc = {day + pd.Timedelta(hours=h): {"p50": v} for h, v in [(6, 90.0), (7, 110.0), (8, 100.0)]}
    points = pipeline.station_points(day + pd.Timedelta(hours=5), 80.0, fc, day)
    assert points == [(330, 80.0), (390, 90.0), (450, 110.0), (510, 100.0)]
    from core import exposure
    curve = exposure.station_curve(points)
    assert curve(450) == 110.0 and curve(420) == 100.0 and curve(480) == 105.0   # 07:30, 07:00, 08:00


def test_no_readings_at_all_fails_cleanly(ctx):
    h = ctx["history"].copy()
    h["pm25"] = np.nan
    run = go(ctx, history=h)
    assert run["status"] == "failed" and run["alert"]["decision"] == "held" and run["forecast"] is None
    json.dumps(run)


def test_after_the_departure_window_no_route_advice(ctx):
    run = go(ctx, now=ctx["now"].normalize() + pd.Timedelta(hours=9, minutes=30))
    assert run["departures"] is None and run["alert"]["reason_code"] in ("after_departure_window", "stale_data")


def test_already_sent_today_is_not_sent_twice(ctx):
    first = go(ctx)
    if first["alert"]["decision"] != "send":
        pytest.skip("this morning does not trigger an alert")
    again = go(ctx, already_sent_today=True)
    assert again["alert"]["decision"] == "none" and again["alert"]["reason_code"] == "already_sent_today"
    assert again["departures"] == first["departures"]


def worst_per_route(ctx, wind_from):
    """Worst value on each route when the wind comes from this direction all morning."""
    h = ctx["history"].copy()
    if "wind_direction_10m" not in h:
        pytest.skip("no wind in the weather file")
    h["wind_direction_10m"] = wind_from % 360
    run = go(ctx, history=h)
    assert run["routes"], f"expected a full run for {ctx['now']}, got {run['steps']}"
    return {r["route_id"]: max(r["summary"]["mean_pm25"]) for r in run["routes"]}


def test_a_route_is_dirtier_when_the_plant_is_upwind_of_it(ctx):
    """Point the wind from the plant at one route, then away from it. Downwind must be dirtier.
    True whatever the town's layout, because the plume only reaches a segment the wind blows toward."""
    town = ctx["town"]
    for rid, segs in town["_segments"].items():
        toward = float(np.mean([s["bearing_from_plant_deg"] for s in segs]))   # the way the wind must blow TO reach it
        downwind = worst_per_route(ctx, toward + 180)[rid]                     # wind FROM the plant's side
        upwind = worst_per_route(ctx, toward)[rid]                             # wind blowing the other way
        assert downwind > upwind, f"{rid}: {downwind} downwind of the plant is not above {upwind} upwind of it"


def straddling_town(town):
    """The same station and models, with two equal routes placed either side of the plant.

    A real town's routes may well lie in the same direction from the plant, in which case no wind can
    separate them. This geometry is built so one is downwind exactly when the other is not."""
    town = copy.deepcopy(town)
    lat, lon = town["plant"]["lat"], town["plant"]["lon"]
    step = 0.0027                          # about 300 m
    feature = lambda rid, order, a, b: {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [a, b]},
                                        "properties": {"segment_id": f"{rid}-{order:02d}", "route_id": rid, "order": order, "road": "local"}}
    features = []
    for rid, side in (("east-side", 1), ("west-side", -1)):
        for order in range(1, 4):
            x = lon + side * 2 * step
            features.append(feature(rid, order, [x, lat + (order - 2) * step], [x, lat + (order - 1) * step]))
    town["routes"] = [{"route_id": "east-side", "name": "East Side Rd", "name_hi": "पूर्वी सड़क", "road": "local"},
                      {"route_id": "west-side", "name": "West Side Rd", "name_hi": "पश्चिमी सड़क", "road": "local"}]
    town["segments_geojson"] = {"type": "FeatureCollection", "features": features}
    town["usual_departure"] = {"route_id": "east-side", "leave": town["usual_departure"]["leave"]}
    return prepare(town)


def test_wind_direction_changes_which_route_is_worse(ctx):
    """Two routes either side of the plant. Point the wind at each in turn: the one it blows toward
    must gain on the other. This is the whole basis of the route advice."""
    ctx = {**ctx, "town": straddling_town(ctx["town"])}
    a, b = list(ctx["town"]["_segments"])
    bearing = {rid: float(np.mean([s["bearing_from_plant_deg"] for s in segs])) for rid, segs in ctx["town"]["_segments"].items()}
    apart = abs((bearing[a] - bearing[b] + 180) % 360 - 180)
    assert apart > 150, f"this geometry should put the routes on opposite sides, got {apart:.0f} degrees apart"
    peaks = {rid: worst_per_route(ctx, bearing[rid] + 180) for rid in (a, b)}
    assert peaks[a][a] > peaks[b][a] and peaks[b][b] > peaks[a][b], "each route is dirtier when the wind points at it"
    assert peaks[a][a] - peaks[a][b] > peaks[b][a] - peaks[b][b]


def test_routes_in_the_same_direction_from_the_plant_cannot_be_told_apart_by_wind(ctx):
    """Why the town file matters: with both routes on one side, the plume ranks them the same whatever the
    wind does, and only distance and road type separate them. Placeholder geometry is usually like this."""
    town = ctx["town"]
    a, b = list(town["_segments"])
    bearing = {rid: float(np.mean([s["bearing_from_plant_deg"] for s in segs])) for rid, segs in town["_segments"].items()}
    apart = abs((bearing[a] - bearing[b] + 180) % 360 - 180)
    if apart > 45:
        pytest.skip(f"{a} and {b} already sit {apart:.0f} degrees apart, so the wind can separate them")
    order = [worst_per_route(ctx, bearing[rid] + 180) for rid in (a, b)]
    assert (order[0][a] > order[0][b]) == (order[1][a] > order[1][b]), \
        f"{a} and {b} are only {apart:.0f} degrees apart as seen from the plant; wind should not reorder them"


def test_many_mornings_run_without_error(ctx):
    seen = set()
    for d in ctx["days"][:: max(1, len(ctx["days"]) // 25)]:
        run = go(ctx, now=d + pd.Timedelta(hours=6, minutes=30))
        json.dumps(run)
        seen.add(run["alert"]["decision"])
        if run["departures"]:
            assert all(ORDER.index(o["peak_band"]) >= 0 for o in run["departures"]["options"])
    assert seen <= {"send", "none", "held"}
