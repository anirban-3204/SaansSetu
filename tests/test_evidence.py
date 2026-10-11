"""The wind-direction evidence: it must find a real direction, and must not invent one."""
import copy
import json

import numpy as np
import pandas as pd
import pytest

from core import api, evidence
from core.store import EVIDENCE, TOWN, MemoryStore, encode
from core.townconfig import prepare

STATION = {"station_id": "s1", "name": "Test station", "lat": 23.0, "lon": 86.0}


def winters(source_deg=None, boost=2.0, seed=1, january_from_north=False, boost_only_when_calm=False, years=(2021, 2022, 2023)):
    """Hourly frame over a few winters. Wind direction is random each day. PM2.5 is multiplied by `boost` when the
    wind comes from within 45 degrees of source_deg, and by nothing else that depends on direction."""
    rng = np.random.default_rng(seed)
    idx = pd.DatetimeIndex([t for y in years for t in pd.date_range(f"{y}-10-01", f"{y + 1}-02-28 23:00", freq="h")])
    day = idx.normalize()
    days = day.unique()
    day_dir = pd.Series(rng.uniform(0, 360, len(days)), index=days)
    wd = (day_dir.reindex(day).to_numpy() + rng.normal(0, 8, len(idx))) % 360
    ws = np.clip(rng.gamma(4.0, 0.6, len(idx)), 0.05, 9)
    month_level = pd.Series({10: 50.0, 11: 80.0, 12: 110.0, 1: 120.0, 2: 90.0})
    pm = month_level.reindex(idx.month).to_numpy() * (1 + 0.2 * (idx.hour == 8)) * np.exp(rng.normal(0, 0.25, len(idx)))
    if january_from_north:
        north = idx.month == 1
        wd = np.where(north, rng.normal(0, 10, len(idx)) % 360, wd)
        pm = np.where(north, pm * 2.0, pm)                        # January is dirtier for reasons that have nothing to do with wind
    if source_deg is not None:
        toward = np.abs((wd - source_deg + 180) % 360 - 180) <= 45
        active = toward & ((ws < 1.0) if boost_only_when_calm else (ws >= 1.0))
        if boost_only_when_calm:            # three days in ten are still all day, and only those are dirty
            still_day = pd.Series(rng.random(len(days)) < 0.3, index=days).reindex(day).to_numpy()
            ws = np.where(still_day, 0.4, np.maximum(ws, 1.5))
            active = still_day
        pm = np.where(active, pm * boost, pm)
    return pd.DataFrame({"pm25": pm, "wind_speed_10m": ws, "wind_direction_10m": wd}, index=idx)


def test_finds_the_direction_the_dirty_air_comes_from():
    stats = evidence.station_stats(winters(source_deg=90), "s1")
    s = stats["top_sector"]
    assert stats["verdict"] == "stands_out" and "E" in s["wind_from"], s
    assert s["lift"] > 1.5 and s["lift_ci95"][0] >= 1.2 and s["lift_ci95"][0] <= s["lift"] <= s["lift_ci95"][1] and s["stable_in"] >= 0.8
    assert s["share_of_high_hours"] > s["share_of_all_hours"] and 0.15 < s["share_of_all_hours"] < 0.35     # two of eight directions
    for source, name in [(0, "N"), (225, "SW"), (315, "NW")]:
        assert name in evidence.station_stats(winters(source_deg=source, seed=source + 2), "s1")["top_sector"]["wind_from"], source


def test_the_table_adds_up():
    frame = winters(source_deg=90)
    stats = evidence.station_stats(frame, "s1")
    p, count = stats["polar"], np.array(stats["polar"]["count"])
    assert count.shape == (5, 8) and len(p["probability"]) == 5 and all(len(r) == 8 for r in p["probability"])
    assert count.sum() == stats["period"]["hours_used"] and stats["period"]["days_used"] > 300
    morning = frame[frame.index.hour.isin([6, 7, 8, 9])]
    assert stats["period"]["hours_used"] <= len(morning) and stats["period"]["hours_used"] > 0.9 * len(morning)
    assert p["wind_from_labels"] == ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] and p["wind_from_deg_bins"][2] == 90 and p["bin_width_deg"] == 45
    prob = np.array([[np.nan if v is None else v for v in row] for row in p["probability"]])
    assert np.nanmin(prob) >= 0 and np.nanmax(prob) <= 1
    east, west = prob[1:, 2], prob[1:, 6]                       # moving air from the east and from the west
    assert np.nanmean(east) > 2 * np.nanmean(west), "cells facing the source are the dirty ones"
    overall = np.nansum(prob * count) / count.sum()
    assert overall == pytest.approx(0.25, abs=0.02), "a quarter of the hours are 'dirtiest' by definition"
    assert stats["threshold"]["pm25_lowest"] < stats["threshold"]["pm25_typical"] < stats["threshold"]["pm25_highest"]


def test_no_source_means_no_direction_is_claimed():
    for seed in [1, 2, 3, 4]:
        stats = evidence.station_stats(winters(source_deg=None, seed=seed), "s1")
        assert stats["verdict"] == "no_clear_direction", (seed, stats["top_sector"])
    doc = evidence.describe(stats, prepare({"town_id": "t", "name": "T", "station": dict(STATION)}), "0.4")
    assert doc["summary"]["en"].startswith("No wind direction stands out") and "from the" not in doc["summary"]["en"]


def test_a_dirty_month_with_its_own_wind_is_not_mistaken_for_a_source():
    """January is twice as dirty and its wind always comes from the north. Wind direction changes nothing within
    any month. Judged against all winter hours together, north would look guilty; judged month by month it does not."""
    stats = evidence.station_stats(winters(source_deg=None, january_from_north=True), "s1")
    assert stats["verdict"] == "no_clear_direction", stats["top_sector"]
    assert stats["threshold"]["pm25_highest"] > 1.8 * stats["threshold"]["pm25_lowest"], "each month has its own bar"


def test_still_air_is_reported_but_never_given_a_direction():
    stats = evidence.station_stats(winters(source_deg=90, boost=3.0, boost_only_when_calm=True), "s1")
    assert stats["calm"]["share_of_high_hours"] > 0.8 and stats["calm"]["share_of_all_hours"] == pytest.approx(0.3, abs=0.06)
    assert stats["verdict"] == "mostly_still_air" and stats["top_sector"] is None and stats["polar"]["count"]
    doc = evidence.describe(stats, prepare({"town_id": "t", "name": "T", "station": dict(STATION)}), "0.4")
    assert doc["summary"]["en"].startswith("The dirtiest winter mornings here are mostly still-air mornings")
    assert "nearly still air" in doc["summary"]["en"] and "थमी हुई हवा" in doc["summary"]["hi"] and "from the" not in doc["summary"]["en"]
    # some still air alongside a real source: the direction is still found, and the still-air share is reported beside it
    mixed = winters(source_deg=90)
    mixed.loc[mixed.index.dayofyear % 5 == 0, "wind_speed_10m"] = 0.3
    both = evidence.station_stats(mixed, "s1")
    assert both["verdict"] == "stands_out" and "E" in both["top_sector"]["wind_from"] and 0.1 < both["calm"]["share_of_all_hours"] < 0.3


def test_wind_for_an_hour_is_the_mean_of_its_start_and_end():
    idx = pd.date_range("2025-01-01 06:00", periods=4, freq="h")
    frame = pd.DataFrame({"pm25": 1.0, "wind_speed_10m": [2.0, 2.0, 3.0, np.nan], "wind_direction_10m": [350.0, 10.0, 90.0, np.nan]}, index=idx)
    speed, deg = evidence.hourly_wind(frame)
    assert min(deg.iloc[0], 360 - deg.iloc[0]) < 0.01, "350 and 10 average to north, not south"
    assert speed.iloc[0] == pytest.approx(2 * np.cos(np.deg2rad(10)), abs=1e-6)
    assert speed.iloc[2] == pytest.approx(3.0) and deg.iloc[2] == pytest.approx(90.0), "no end reading: the start is used"
    assert np.isnan(speed.iloc[3])
    east = pd.DataFrame({"pm25": 1.0, "wind_speed_10m": [4.0, 4.0], "wind_direction_10m": [90.0, 90.0]}, index=idx[:2])
    assert evidence.hourly_wind(east)[1].iloc[0] == pytest.approx(90.0), "a wind from the east stays a wind from the east"


def test_too_little_data_says_so():
    short = winters(source_deg=90).iloc[:600]
    stats = evidence.station_stats(short, "s1")
    assert stats["verdict"] == "not_enough_data" and stats["top_sector"] is None and "needs 300" in stats["why"]
    doc = evidence.describe(stats, prepare({"town_id": "t", "name": "T", "station": dict(STATION)}), "0.4")
    assert doc["summary"]["en"].startswith("There are too few") and doc["plant"] is None
    nowind = evidence.station_stats(winters().drop(columns=["wind_direction_10m"]), "s1")
    assert nowind["verdict"] == "not_enough_data" and "no wind" in nowind["why"]
    encode(doc), encode(evidence._plain(nowind))


def routed(plant, **flags):
    raw = json.loads((__import__("conftest").FIX / "towns" / "testville.json").read_text(encoding="utf-8"))
    raw["station"] = dict(STATION)
    raw["plant"] = {"name": "Acme Steel Works", **plant, **flags}
    return prepare(raw)


def test_the_plant_is_mentioned_only_when_its_place_is_real_and_never_by_name():
    stats = evidence.station_stats(winters(source_deg=90), "s1")
    east, west = {"lat": 23.0, "lon": 86.05}, {"lat": 23.0, "lon": 85.95}
    same_side = evidence.describe(stats, routed(east), "0.4")
    assert same_side["plant"] == {"bearing_from_station_deg": 90, "label": "E", "location_is_placeholder": False, "inside_top_sector": True}
    assert "that is the side the industrial area lies on" in same_side["summary"]["en"] and "औद्योगिक क्षेत्र उसी ओर है" in same_side["summary"]["hi"]
    other_side = evidence.describe(stats, routed(west), "0.4")
    assert other_side["plant"]["inside_top_sector"] is False and "does not lie on that side" in other_side["summary"]["en"]
    guess = evidence.describe(stats, routed(east, approximate=True), "0.4")
    assert guess["plant"]["location_is_placeholder"] and "industrial" not in guess["summary"]["en"] and "औद्योगिक" not in guess["summary"]["hi"]
    assert any("placeholder" in c for c in guess["caveats"]["en"]) and len(guess["caveats"]["hi"]) == len(guess["caveats"]["en"]) == 3
    none = evidence.describe(stats, prepare({"town_id": "t", "name": "T", "station": dict(STATION)}), "0.4")
    assert none["plant"] is None and "industrial" not in none["summary"]["en"] and len(none["caveats"]["en"]) == 2
    for doc in (same_side, other_side, guess, none):
        text = encode(doc)                                   # valid JSON, no NaN
        assert "Acme" not in text, "no company is ever named"
        assert doc["caveats"]["en"][0] == "One station and area wind show a direction, not proof of a source."
        assert doc["town_id"] and doc["schema_version"] == "0.4" and doc["is_sample"] is False
    assert "north-east to east" in same_side["summary"]["en"] or "east to south-east" in same_side["summary"]["en"]


def test_same_input_gives_the_same_answer():
    frame = winters(source_deg=135)
    assert evidence.station_stats(frame, "s1") == evidence.station_stats(copy.deepcopy(frame), "s1")


def test_the_api_serves_it_and_says_how_to_make_it_when_missing():
    store = MemoryStore()
    store.put(TOWN, "all", "asansol", {"town_id": "asansol", "name": "Asansol", "has_routes": True})
    status, body = api.handle(store, "GET", "/towns/asansol/evidence")
    assert status == 404 and "make_evidence.py" in body["error"]
    doc = evidence.describe(evidence.station_stats(winters(source_deg=90), "s1"), prepare({"town_id": "asansol", "name": "Asansol", "station": dict(STATION)}), "0.4")
    store.put(EVIDENCE, "asansol", "current", doc)
    assert api.handle(store, "GET", "/towns/asansol/evidence") == (200, doc)


def test_each_hour_lands_in_the_right_cell():
    """Wind from 350 degrees is a north wind, not a north-west one: directions are centred on the compass point."""
    def cell(deg, speed):
        frame = winters(source_deg=None)
        frame["wind_direction_10m"], frame["wind_speed_10m"] = float(deg), float(speed)
        count = np.array(evidence.station_stats(frame, "s1")["polar"]["count"])
        assert (count > 0).sum() == 1, "steady wind: every hour in one cell"
        row, col = np.argwhere(count > 0)[0]
        return int(row), evidence.DIRS[col]
    assert cell(350, 2.5) == (2, "N") and cell(10, 2.5) == (2, "N") and cell(22.4, 2.5) == (2, "N")
    assert cell(23, 2.5) == (2, "NE") and cell(90, 2.5) == (2, "E") and cell(200, 2.5) == (2, "S") and cell(337, 2.5) == (2, "NW")
    assert cell(90, 0.4)[0] == 0 and cell(90, 1.0)[0] == 1 and cell(90, 3.99)[0] == 3 and cell(90, 4.0)[0] == 4
    assert cell(90, 9.0)[0] == 4, "anything faster than the last edge goes in the last band"
