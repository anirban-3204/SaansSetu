"""The helper scripts: town files from trained stations, routes from a drawing, the deploy bundle."""
import copy
import json
import shutil
import subprocess
import sys

import pytest

import make_routes
import make_towns
from core import runner
from core.geo import distance_m
from core.townconfig import load_town, load_towns, prepare
from conftest import DATA, FIX, ROOT, TOWNS


# ---------------------------------------------------------------- make_routes

def drawing(reverse_second=False, far_school=False):
    """Two bent routes from one home to one school, as geojson.io would save them."""
    home, school = [81.3640, 21.2010], [81.3615, 21.1850]
    a = [home, [81.3690, 21.1975], [81.3700, 21.1915], [81.3665, 21.1870], school]
    b = [home, [81.3590, 21.1975], [81.3570, 21.1915], [81.3580, 21.1872], school]
    point = lambda role, xy, **p: {"type": "Feature", "properties": {"role": role, **p}, "geometry": {"type": "Point", "coordinates": xy}}
    line = lambda xy, **p: {"type": "Feature", "properties": p, "geometry": {"type": "LineString", "coordinates": xy}}
    return {"type": "FeatureCollection", "features": [
        line(a, name="Plant Gate Rd", name_hi="प्लांट गेट रोड", road="main"), line(b[::-1] if reverse_second else b, name="Station Rd"),
        point("school", [81.40, 21.25] if far_school else school, name="St. Example School"), point("plant", [81.378, 21.188]),
        point("Home", home)]}


def test_cut_makes_equal_pieces_that_join_up_and_keep_the_bends():
    line = [[81.0, 21.0], [81.004, 21.0], [81.004, 21.003], [81.010, 21.003]]
    pieces, total = make_routes.cut(line, 100)
    length = lambda piece: sum(distance_m((a[1], a[0]), (b[1], b[0])) for a, b in zip(piece, piece[1:]))
    assert total == pytest.approx(length(line), rel=1e-6) and len(pieces) == round(total / 100)
    assert all(length(p) == pytest.approx(total / len(pieces), rel=0.01) for p in pieces), "no short stub at the end"
    assert pieces[0][0] == line[0] and pieces[-1][-1] == line[-1]
    assert all(a[-1] == b[0] for a, b in zip(pieces, pieces[1:])), "each piece starts where the last one ended"
    assert sum(length(p) for p in pieces) == pytest.approx(total, rel=1e-4)
    assert any([81.004, 21.0] in p for p in pieces) and any([81.004, 21.003] in p for p in pieces)
    assert len(make_routes.cut([[81.0, 21.0], [81.0002, 21.0]], 100)[0]) == 1      # shorter than one piece: still one segment
    with pytest.raises(ValueError):
        make_routes.cut([[81.0, 21.0], [81.0, 21.0]], 100)


def test_drawn_routes_replace_the_placeholders_and_the_file_still_loads():
    raw = json.loads((FIX / "towns" / "testville.json").read_text(encoding="utf-8"))
    new, notes = make_routes.build(raw, drawing())
    town = prepare(copy.deepcopy(new))
    assert [r["route_id"] for r in new["routes"]] == ["plant-gate-rd", "station-rd"] and new["usual_departure"] == {"route_id": "plant-gate-rd", "leave": "07:20"}
    assert new["routes"][0]["name_hi"] == "प्लांट गेट रोड" and new["routes"][1]["name_hi"] == "Station Rd" and new["routes"][1]["road"] == "local"
    for rid, segs in town["_segments"].items():
        assert 15 < len(segs) < 30 and all(80 < s["length_m"] < 120 for s in segs)
    assert "placeholder" not in new["school"] and new["school"]["name"] == "St. Example School" and new["school"]["starts_at"] == "08:00"
    assert "approximate" not in new["plant"] and new["plant"]["lat"] == 21.188 and "PLACEHOLDER" not in new["geometry_note"]
    assert "timezone_offset" not in new and "_segments" not in new and "has_routes" not in new, "defaults are not frozen into the file"
    assert any("Station Rd: no road type given" in n for n in notes) and any("no name_hi" in n for n in notes)
    assert raw["routes"][0]["route_id"] == "plant-gate", "the input is not modified"


def test_a_route_drawn_backwards_is_turned_round_and_problems_are_said_plainly():
    raw = json.loads((FIX / "towns" / "testville.json").read_text(encoding="utf-8"))
    new, notes = make_routes.build(raw, drawing(reverse_second=True))
    firsts = [f["geometry"]["coordinates"][0] for f in new["segments_geojson"]["features"] if f["properties"]["order"] == 1]
    assert firsts[0] == firsts[1] == [81.364, 21.201] and any("reversed" in n for n in notes)
    _, notes = make_routes.build(raw, drawing(far_school=True))
    assert any("from the school marker" in n for n in notes)
    one_line = drawing()
    one_line["features"].pop(1)
    with pytest.raises(ValueError, match="at least two routes"):
        make_routes.build(raw, one_line)
    no_plant = drawing()
    no_plant["features"].pop(3)
    with pytest.raises(ValueError, match="role = plant"):
        make_routes.build(raw, no_plant)


def test_routes_can_be_added_to_a_forecast_only_town_and_the_script_keeps_a_backup(tmp_path):
    shutil.copy(FIX / "towns" / "otherton.json", tmp_path / "otherton.json")
    (tmp_path / "map.geojson").write_text(json.dumps(drawing()), encoding="utf-8")
    done = subprocess.run([sys.executable, str(ROOT / "make_routes.py"), str(tmp_path / "otherton.json"), str(tmp_path / "map.geojson")],
                          cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-500:]
    town = load_town(tmp_path / "otherton.json")
    assert town["has_routes"] and len(town["_segments"]) == 2 and town["alert_label"] == "poor"
    assert json.loads((tmp_path / "otherton.json.bak").read_text(encoding="utf-8")) == json.loads((FIX / "towns" / "otherton.json").read_text(encoding="utf-8"))
    assert set(load_towns(tmp_path)[0]) == {"otherton"}, "the .bak file is not picked up as a town"


# ---------------------------------------------------------------- make_towns and pack_models (need data and models)

def test_make_towns_writes_one_file_per_passed_station_and_keeps_existing_ones(world, tmp_path):
    if not (DATA / "stations.csv").exists():
        pytest.skip("no stations.csv")
    towns_dir = tmp_path / "towns"
    towns_dir.mkdir()
    hero_file = next(p for p in TOWNS.glob("*.json") if json.loads(p.read_text(encoding="utf-8")).get("town_id") == world.hero["town_id"])
    shutil.copy(hero_file, towns_dir / hero_file.name)
    before = (towns_dir / hero_file.name).read_text(encoding="utf-8")
    files, rows, problems = make_towns.build(world.root, DATA, towns_dir)
    assert problems == {} and (towns_dir / hero_file.name).read_text(encoding="utf-8") == before
    kept = [r for r in rows if r["file"].startswith("kept")]
    assert [r["station_id"] for r in kept] == [world.hero["station"]["station_id"]]
    trained = {p.parent.name for p in world.root.glob("*/config.json")}
    assert {r["station_id"] for r in rows} == trained
    for path, town in files.items():
        assert set(town) <= {"town_id", "name", "state", "station", "alert_label"} and path.name == f"{town['town_id']}.json"
        assert prepare(copy.deepcopy(town))["has_routes"] is False and -90 < town["station"]["lat"] < 90
        cfg = json.loads((world.root / town["station"]["station_id"] / "config.json").read_text())
        assert cfg["verdict_level"] == "SHIP" and town["alert_label"] in cfg["labels"]
    ids = [t["town_id"] for t in files.values()]
    assert len(ids) == len(set(ids)) and world.hero["town_id"] not in ids
    again = subprocess.run([sys.executable, str(ROOT / "make_towns.py"), "--models", str(world.root), "--data", str(DATA), "--towns", str(towns_dir)],
                           cwd=ROOT, capture_output=True, text=True)
    assert again.returncode == 0, again.stderr[-500:]
    assert len(load_towns(towns_dir)[0]) == 1 + len(files)
    second, rows2, _ = make_towns.build(world.root, DATA, towns_dir)
    assert second == {} and all(r["file"].startswith(("kept", "skipped")) for r in rows2), "running it twice changes nothing"


def test_packed_bundle_is_enough_to_run_the_system(world, tmp_path):
    towns_dir = tmp_path / "towns"
    towns_dir.mkdir()
    for p in TOWNS.glob("*.json"):
        if json.loads(p.read_text(encoding="utf-8")).get("town_id") in world.towns:
            shutil.copy(p, towns_dir / p.name)
    out = tmp_path / "deploy"
    done = subprocess.run([sys.executable, str(ROOT / "pack_models.py"), "--models", str(world.root), "--data", str(DATA),
                           "--towns", str(towns_dir), "--out", str(out), "--days", world.alert_day, world.alert_day],
                          cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]
    sid = world.hero["station"]["station_id"]
    has_final = all((world.root / t["station"]["station_id"] / "final" / "calibration.json").exists() for t in world.towns.values())
    assert ("PROBLEM" not in done.stdout) == has_final, done.stdout   # a station trained without --final is called out
    assert not (out / "models" / sid / "models" / "fut").exists() and (out / "models" / sid / "models" / "now" / "p50_h1.txt").exists()
    assert not (out / "models" / sid / "backtest.csv").exists()
    towns, problems = load_towns(out / "towns")
    assert set(towns) == set(world.towns) and not problems
    # the wind-direction table is worked out from the full archive and saved beside the packed models
    import make_evidence
    from core import api
    made = make_evidence.build_all(towns, DATA, out / "models")
    assert set(made) == set(towns) and all((out / "models" / t["station"]["station_id"] / "evidence.json").exists() for t in towns.values())
    packed_store, packed = runner.replay_day(towns, out / "saanssetu_data", out / "models", world.alert_day, start="06:00", end="06:30")
    for town_id in towns:
        status, doc = api.handle(packed_store, "GET", f"/towns/{town_id}/evidence")
        assert status == 200 and doc == made[town_id] and doc["town_id"] == town_id
        assert doc["verdict"] in ("stands_out", "no_clear_direction", "mostly_still_air", "not_enough_data") and doc["summary"]["hi"]
        assert all("evidence_problem" not in r for r in packed[town_id])
    _, full = runner.replay_day(world.copy_towns(), DATA, world.root, world.alert_day, start="06:00", end="06:30")
    for town_id in world.towns:
        for a, b in zip(packed[town_id], full[town_id]):
            assert a["status"] == b["status"] and a["forecast"] == b["forecast"] and a["alert"] == b["alert"], town_id
    if has_final:
        live = runner.run_town(runner.MemoryStore(), towns[world.hero["town_id"]], out / "models",
                               runner.ingest.ArchiveSource(out / "saanssetu_data"), f"{world.alert_day} 06:30", mode="replay", model_set="final")
        assert live["status"] == "ok", "the deployment models are in the bundle too"


# ---------------------------------------------------------------- probe_live (the parts that need no network)

def openaq_hours(series, shift_hours=0, scale=1.0):
    """A stored hourly series as OpenAQ's /hours endpoint would return it."""
    import pandas as pd
    out = []
    for t, v in series.items():
        end = t + pd.Timedelta(hours=shift_hours) - pd.Timedelta(hours=5, minutes=30)   # to UTC
        iso = lambda x: x.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"value": float(v) * scale, "period": {"datetimeFrom": {"utc": iso(end - pd.Timedelta(hours=1))}, "datetimeTo": {"utc": iso(end)}}})
    return out + [{"value": 1.0, "period": None}]   # a malformed row must be skipped, not crash


def test_probe_tells_stamp_convention_and_unit_mismatch_apart():
    import numpy as np
    import pandas as pd
    import probe_live
    rng = np.random.default_rng(3)
    idx = pd.date_range("2025-09-20", periods=240, freq="h")
    archive = pd.Series(80 + np.cumsum(rng.normal(0, 6, 240)), index=idx).clip(lower=5)
    by_start, by_end = probe_live.hours_to_series(openaq_hours(archive))                 # archive stamps = OpenAQ period end
    assert by_end.index[0] == idx[0] and by_start.index[0] == idx[0] - pd.Timedelta(hours=1)
    v = probe_live.verdict(probe_live.compare(archive, by_start, by_end))
    assert v.startswith("MATCHES the archive when stamped by hour end") and "same units" in v
    v = probe_live.verdict(probe_live.compare(archive, *probe_live.hours_to_series(openaq_hours(archive, shift_hours=1))))
    assert v.startswith("MATCHES the archive when stamped by hour start")
    v = probe_live.verdict(probe_live.compare(archive, *probe_live.hours_to_series(openaq_hours(archive, scale=0.5))))
    assert "DIFFERENT SCALE" in v
    noise = pd.Series(rng.uniform(20, 200, 240), index=idx)
    assert probe_live.verdict(probe_live.compare(archive, *probe_live.hours_to_series(openaq_hours(noise)))).startswith("DOES NOT MATCH")
    assert probe_live.verdict(probe_live.compare(archive, *probe_live.hours_to_series(openaq_hours(archive.head(5))))).startswith("NOT ENOUGH OVERLAP")
    assert probe_live.naive_ist("2026-10-06T21:00:00+05:30") == pd.Timestamp("2026-10-06 21:00")
    # Asansol on 7 Oct 2026: the location lists PM2.5 twice and the first sensor is an empty old one. Use the one with data.
    sensor, results = probe_live.fullest({14835: [], 99999: openaq_hours(archive.head(48)), 55555: openaq_hours(archive.head(5))})
    assert sensor == 99999 and len(results) == 49


def test_make_towns_skips_stations_that_did_not_pass_or_have_no_coordinates(tmp_path):
    """Made-up results folder, no real data needed: only a station that passed and can be placed gets a town file."""
    import pandas as pd
    models, data, towns_dir = tmp_path / "out", tmp_path / "data", tmp_path / "towns"
    data.mkdir()
    towns_dir.mkdir()
    cfg = lambda sid, name, level, labels, suggested: {"station_id": sid, "station": name, "verdict_level": level, "verdict_chance": "USE",
                                                       "labels": labels, "suggested_label": suggested}
    from core import check
    passed = lambda label, low: {"now": {check.CASE: {"chance": {label: {"skill_vs_usual_rate": 0.4, "skill_vs_usual_rate_ci95": [low, 0.6]}}}}}
    stations = [("site_1", "Raghunathpali, Rourkela - OSPCB", "SHIP", ["moderate", "poor"], "moderate", passed("poor", 0.2)),
                ("site_2", "Civic Center, Bhilai - CECB", "DO NOT SHIP", ["poor"], "poor", passed("poor", 0.2)),
                ("site_3", "Nowhere Road, Lostville - XPCB", "SHIP", ["poor"], "poor", passed("poor", 0.2)),
                ("site_4", "GVM Corporation, Visakhapatnam - APPCB", "SHIP", ["moderate", "poor"], "moderate", passed("poor", -0.1)),
                ("site_5", "Second Site, Rourkela - OSPCB", "SHIP", ["moderate"], "moderate", None),
                # train.py said SHIP on the 6-to-7 AM reading, but the alert people can use (5-to-6 AM reading) does not clear the bar
                ("site_6", "Late Road, Slowtown - XPCB", "SHIP", ["poor"], "poor",
                 {"now": {check.CASE: {"skill_vs_persistence": 0.04, "skill_vs_persistence_ci95": [-0.05, 0.12], "chance": {}}}})]
    for sid, name, level, labels, suggested, metrics in stations:
        (models / sid).mkdir(parents=True)
        (models / sid / "config.json").write_text(json.dumps(cfg(sid, name, level, labels, suggested)))
        if metrics:
            (models / sid / "metrics.json").write_text(json.dumps(metrics))
    pd.DataFrame([{"station_id": "site_1", "station_name": "x", "latitude": 22.2, "longitude": 84.8},
                  {"station_id": "site_2", "station_name": "x", "city_name": "Bhilai", "latitude": 21.2, "longitude": 81.3},
                  {"station_id": "site_3", "station_name": "x", "latitude": None, "longitude": None},
                  {"station_id": "site_4", "station_name": "x", "city_name": "Visakhapatnam", "latitude": 17.7, "longitude": 83.3},
                  {"station_id": "site_5", "station_name": "x", "city_name": "Rourkela", "latitude": 22.25, "longitude": 84.85},
                  {"station_id": "site_6", "station_name": "x", "city_name": "Slowtown", "latitude": 22.0, "longitude": 84.0}]
                 ).to_csv(data / "stations.csv", index=False)
    files, rows, problems = make_towns.build(models, data, towns_dir)
    towns = {t["station"]["station_id"]: t for t in files.values()}
    why = {r["station_id"]: r["file"] for r in rows}
    assert set(towns) == {"site_1", "site_4", "site_5"} and problems == {}
    assert "did not pass" in why["site_2"] and "no coordinates" in why["site_3"]
    assert "did not pass" in why["site_6"], "judged on the 6 AM alert, not on train.py's stored verdict"
    shown = {r["station_id"]: r for r in rows}
    assert shown["site_6"]["level"] == "DO NOT SHIP" and shown["site_6"]["6am_alert_skill"] == 0.04 and shown["site_6"]["ci95"] == [-0.05, 0.12]
    assert towns["site_1"]["town_id"] == "rourkela" and towns["site_1"]["name"] == "Rourkela", "city read from the station name"
    assert towns["site_1"]["station"]["name"] == "Raghunathpali, Rourkela" and towns["site_1"]["alert_label"] == "poor"
    assert towns["site_4"]["alert_label"] == "moderate", "poor did not pass its test there, so the suggestion is kept"
    assert towns["site_5"]["town_id"] == "rourkela-site-5" and towns["site_5"]["alert_label"] == "moderate", "two stations in one city get different ids"
    assert {p.name for p in files} == {"rourkela.json", "visakhapatnam.json", "rourkela-site-5.json"}


def test_the_bundle_carries_the_evidence_file(tmp_path):
    import pack_models
    src = tmp_path / "out" / "site_9"
    (src / "models" / "now").mkdir(parents=True)
    for name in ("config.json", "metrics.json", "evidence.json", "backtest.csv"):
        (src / name).write_text("{}")
    (src / "models" / "calibration.json").write_text("{}")
    (src / "models" / "now" / "p50_h1.txt").write_text("x")
    problem = pack_models.pack_station(tmp_path / "out", "site_9", tmp_path / "deploy")
    packed = tmp_path / "deploy" / "models" / "site_9"
    assert {p.name for p in packed.iterdir()} == {"config.json", "metrics.json", "evidence.json", "models"}
    assert "missing final" in problem, "a station trained without --final is still called out"
