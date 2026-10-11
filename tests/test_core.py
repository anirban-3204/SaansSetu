"""Tests for the parts that need no data or models: bands, geometry, exposure, departure, alert, town files."""
import copy
import json
import math
from pathlib import Path

import pytest

from core import alert as alerts
from core import departure, exposure
from core.bands import at_least, band
from core.geo import bearing_deg, compass, distance_m, hhmm, minutes
from core.townconfig import ConfigError, load_town, prepare

FIX = Path(__file__).parent / "fixtures"
PARAMS = {"plume_strength": 0.9, "plume_decay_m": 900, "traffic_strength": {"main": 0.25, "local": 0.08},
          "traffic_peak": "07:45", "traffic_width_minutes": 35}


@pytest.fixture
def town():
    return load_town(FIX / "towns" / "testville.json")


# ---------------------------------------------------------------- bands and geometry

def test_bands_follow_india_aqi_breakpoints():
    assert [band(v) for v in [0, 30, 31, 60, 61, 90, 91, 120, 121, 250, 251, 900]] == \
        ["good", "good", "satisfactory", "satisfactory", "moderate", "moderate", "poor", "poor", "very_poor", "very_poor", "severe", "severe"]
    assert band(None) is None and band(float("nan")) is None
    assert at_least("very_poor", "poor") and at_least("poor", "poor") and not at_least("moderate", "poor") and not at_least(None, "poor")


def test_geometry():
    a, north, east = (23.0, 86.0), (23.01, 86.0), (23.0, 86.01)
    assert distance_m(a, north) == pytest.approx(1112, rel=0.01)
    assert bearing_deg(a, north) == pytest.approx(0, abs=0.01) and bearing_deg(a, east) == pytest.approx(90, abs=0.1)
    assert [compass(d) for d in [0, 45, 90, 180, 270, 359]] == ["N", "NE", "E", "S", "W", "N"]
    assert minutes("07:45") == 465 and hhmm(465) == "07:45" and hhmm(minutes("06:05")) == "06:05"


# ---------------------------------------------------------------- exposure

def seg(bearing, dist, road="local"):
    return {"bearing_from_plant_deg": bearing, "distance_to_plant_m": dist, "road": road, "length_m": 300}


def test_station_curve_joins_points_and_holds_the_ends():
    c = exposure.station_curve([(360, 100), (420, 160), (480, 130)])
    assert c(300) == 100 and c(360) == 100 and c(390) == 130 and c(420) == 160 and c(450) == 145 and c(600) == 130


def test_plume_is_downwind_only_and_fades_with_distance():
    # wind FROM the west (270) blows TO the east (90); a segment due east of the plant is downwind
    assert exposure.plume_factor(seg(90, 500), 270, PARAMS) > exposure.plume_factor(seg(90, 2000), 270, PARAMS) > 0
    assert exposure.plume_factor(seg(270, 500), 270, PARAMS) == 0          # upwind
    assert exposure.plume_factor(seg(0, 500), 270, PARAMS) == pytest.approx(0, abs=1e-9)   # crosswind
    assert exposure.plume_factor(seg(90, 500), None, PARAMS) == 0          # no wind reading
    assert exposure.plume_factor(seg(90, 0), 270, PARAMS) == pytest.approx(PARAMS["plume_strength"])


def test_traffic_peaks_at_rush_hour_and_is_larger_on_main_roads():
    peak = minutes("07:45")
    assert exposure.traffic_factor(seg(0, 5000, "main"), peak, PARAMS) == pytest.approx(0.25)
    assert exposure.traffic_factor(seg(0, 5000, "local"), peak, PARAMS) == pytest.approx(0.08)
    assert exposure.traffic_factor(seg(0, 5000, "main"), peak - 120, PARAMS) < 0.001
    assert exposure.traffic_factor(seg(0, 5000, "footpath"), peak, PARAMS) == 0


def test_segment_is_never_below_the_station_value():
    c = exposure.station_curve([(360, 100)])
    for b in range(0, 360, 30):
        for m in range(360, 540, 15):
            assert exposure.segment_pm25(seg(b, 800, "main"), m, c, 200, PARAMS) >= 100


# ---------------------------------------------------------------- departure

def test_dose_is_concentration_times_minutes(town):
    segs = town["_segments"]["plant-gate"]
    total_min = sum(s["length_m"] for s in segs) / 1000 / town["travel"]["speed_kmh"] * 60
    t = departure.trip("plant-gate", segs, minutes("07:00"), town["travel"]["speed_kmh"], lambda s, m: 100.0, minutes("08:00"))
    assert t["dose"] == pytest.approx(100 * total_min, abs=1) and t["avg_pm25"] == 100 and t["peak_pm25"] == 100
    assert t["minutes"] == round(total_min) and t["leave"] == "07:00" and t["arrives_on_time"]


def test_compare_picks_the_lowest_dose_that_arrives_on_time(town):
    rising = lambda s, m: 50.0 + (m - 400) + (40 if s["route_id"] == "plant-gate" else 0)   # worse later, worse near the plant
    d = departure.compare(town, rising)
    on_time = [o for o in d["options"] if o["arrives_on_time"]]
    assert d["best"] == min(on_time, key=lambda o: (o["dose"], o["leave"]))
    assert d["best"]["route_id"] == "station-rd" and d["best"]["leave"] == "07:00"
    assert d["saving_vs_usual_pct"] == round((1 - d["best"]["dose"] / d["usual"]["dose"]) * 100)
    assert 0 < d["saving_from_route_change_only_pct"] <= d["saving_vs_usual_pct"]
    assert len(d["options"]) == 2 * 8 and d["usual"]["leave"] == "07:20"


def test_compare_never_advises_leaving_in_the_past(town):
    d = departure.compare(town, lambda s, m: 100.0, not_before_minute=minutes("07:12"))
    assert min(o["leave"] for o in d["options"]) == "07:15"
    assert departure.compare(town, lambda s, m: 100.0, not_before_minute=minutes("06:30"))["options"][0]["leave"] == "07:00"


def test_compare_with_no_on_time_option(town):
    late = copy.deepcopy(town)
    late["school"]["starts_at"] = "07:05"
    d = departure.compare(late, lambda s, m: 100.0)
    assert d["best"] is None and d["saving_vs_usual_pct"] is None


# ---------------------------------------------------------------- alert

def dep(saving, best_route="station-rd"):
    return {"best": {"route_id": best_route, "leave": "07:00", "dose": 100}, "usual": {"route_id": "plant-gate", "leave": "07:20", "dose": 140},
            "saving_vs_usual_pct": saving}


PEAK = {"time": "07:45", "pm25": 150, "band": "very_poor"}


def test_alert_sends_when_saving_and_chance_clear_the_rule(town):
    a = alerts.decide(town, dep(28), PEAK, "poor", 0.71, True, False, "2025-01-14T06:30+05:30", recipients=3)
    assert a["decision"] == "send" and a["recipients"] == 3 and a["sent_at"] == "2025-01-14T06:30+05:30"
    assert a["message"]["en"] == "Plant Gate Rd will likely turn Very Poor around 7:45. Take Station Rd, and leave by 7:00."
    assert "प्लांट गेट रोड" in a["message"]["hi"] and "स्टेशन रोड" in a["message"]["hi"] and "7:00" in a["message"]["hi"]
    assert a["school_flag"]["outdoor_pt"] == "move_indoors"


def test_alert_same_route_only_changes_the_time(town):
    a = alerts.decide(town, dep(25, best_route="plant-gate"), PEAK, "poor", 0.9, True, False, "x")
    assert a["message"]["en"].endswith("Leave by 7:00.") and "Take" not in a["message"]["en"]


@pytest.mark.parametrize("saving,chance,bad,sent,code", [
    (12, 0.9, True, False, "no_meaningful_difference"),
    (30, 0.2, False, False, "low_chance"),
    (30, 0.9, True, True, "already_sent_today"),
])
def test_alert_stays_quiet(town, saving, chance, bad, sent, code):
    a = alerts.decide(town, dep(saving), PEAK, "poor", chance, bad, sent, "x")
    assert a["decision"] == "none" and a["reason_code"] == code and a["message"] is None and a["recipients"] == 0
    assert a["school_flag"]["outdoor_pt"] == ("move_indoors" if bad else "ok")   # the school flag does not depend on the saving


def test_alert_without_a_chance_model_falls_back_to_the_band(town):
    a = alerts.decide(town, dep(30), PEAK, None, None, True, False, "x")
    assert a["decision"] == "send" and "Very Poor" in a["reason"]


def test_alert_with_no_on_time_option_and_held(town):
    d = {"best": None, "usual": {"route_id": "plant-gate", "leave": "07:20", "dose": 1}, "saving_vs_usual_pct": None}
    assert alerts.decide(town, d, PEAK, "poor", 0.9, True, False, "x")["reason_code"] == "no_on_time_option"
    h = alerts.held("stale_data", "old")
    assert h["decision"] == "held" and h["school_flag"]["outdoor_pt"] == "unknown" and h["message"] is None


# ---------------------------------------------------------------- town files

def test_town_file_is_prepared(town):
    assert set(town["_segments"]) == {"plant-gate", "station-rd"}
    for segs in town["_segments"].values():
        assert [s["order"] for s in segs] == sorted(s["order"] for s in segs)
        assert all(s["length_m"] > 0 and s["distance_to_plant_m"] > 0 and 0 <= s["bearing_from_plant_deg"] < 360 for s in segs)
    nearest = {rid: min(s["distance_to_plant_m"] for s in segs) for rid, segs in town["_segments"].items()}
    assert nearest["plant-gate"] < nearest["station-rd"]
    assert town["alert_rule"]["min_saving_pct"] == 20 and town["slots"]["step_minutes"] == 15       # defaults filled in


def test_bad_town_files_are_rejected():
    raw = json.loads((FIX / "towns" / "testville.json").read_text(encoding="utf-8"))
    for breaker in [lambda t: t.pop("plant"), lambda t: t["usual_departure"].update(route_id="nope"),
                    lambda t: t["segments_geojson"]["features"][0]["properties"].update(route_id="ghost"),
                    lambda t: t["routes"].append({"route_id": "empty", "name": "Empty"})]:
        t = copy.deepcopy(raw)
        breaker(t)
        with pytest.raises((ConfigError, KeyError)):
            prepare(t)


def test_asansol_town_file_loads():
    t = load_town(Path(__file__).parent.parent / "towns" / "asansol.json")
    assert t["station"]["station_id"] == "site_1418" and len(t["_segments"]) == 2
    assert all(r.get("name_hi") for r in t["routes"])
