"""Loads town files and works out each route segment's length, distance and bearing from the plant.

Two kinds of town:
  with routes     station + plant + school + routes: forecast, route advice, parent alert, school flag
  forecast only   station only: forecast and school flag. No plant, no routes, no parent alert.
"""
import json
from pathlib import Path

from .bands import BANDS
from .geo import bearing_deg, distance_m

DEFAULTS = {
    "timezone_offset": "+05:30",
    "stale_after_minutes": 120,
    "school_hours": [7, 8, 9],
    "travel": {"mode": "walk", "speed_kmh": 4.5},
    "slots": {"start": "06:45", "end": "08:30", "step_minutes": 15},
    "departure_window": {"earliest": "07:00", "latest": "07:35", "step_minutes": 5},
    "alert_rule": {"min_saving_pct": 20, "min_chance": 0.6, "earliest": "06:00"},
    "exposure_params": {"plume_strength": 0.9, "plume_decay_m": 900,
                        "traffic_strength": {"main": 0.25, "local": 0.08},
                        "traffic_peak": "07:45", "traffic_width_minutes": 35},
}
ROUTE_KEYS = ["plant", "school", "routes", "segments_geojson", "usual_departure"]
CREDITS = ["CPCB CAAQM network via India Air Quality Database, XKDR Forum (CC BY 4.0)",
           "Weather: Open-Meteo (CC BY 4.0)", "Map data: OpenStreetMap contributors"]


class ConfigError(ValueError):
    pass


def load_town(path):
    town = json.loads(Path(path).read_text(encoding="utf-8"))
    return prepare(town)


def load_towns(folder):
    """Every town file in a folder. Returns ({town_id: town}, {file name: what is wrong with it}).
    A broken file is reported and skipped, so one bad town never stops the others."""
    towns, problems = {}, {}
    for path in sorted(Path(folder).glob("*.json")):
        try:
            town = load_town(path)
            if town["town_id"] in towns:
                raise ConfigError(f"town_id '{town['town_id']}' is used by two files")
            towns[town["town_id"]] = town
        except (ConfigError, KeyError, TypeError, ValueError) as e:
            problems[path.name] = f"{type(e).__name__}: {e}"
    return towns, problems


def prepare(town):
    """Fills defaults, checks the file, and attaches computed segment facts under town['_segments']."""
    for key, value in DEFAULTS.items():
        if isinstance(value, dict):
            town[key] = {**value, **town.get(key, {})}
        else:
            town.setdefault(key, value)
    for key in ["town_id", "name", "station"]:
        if key not in town:
            raise ConfigError(f"town file is missing '{key}'")
    if "station_id" not in town["station"]:
        raise ConfigError("station is missing 'station_id'")

    town["has_routes"] = bool(town.get("routes"))
    if not town["has_routes"]:
        if town.get("segments_geojson", {}).get("features"):
            raise ConfigError("town file has route segments but no 'routes'")
        town["school"] = {"starts_at": "08:00", **town.get("school", {})}
        town["_segments"] = {}
        return town

    for key in ROUTE_KEYS:
        if key not in town:
            raise ConfigError(f"town file is missing '{key}'")
    plant = (town["plant"]["lat"], town["plant"]["lon"])
    routes = {r["route_id"]: r for r in town["routes"]}
    if town["usual_departure"]["route_id"] not in routes:
        raise ConfigError("usual_departure.route_id is not one of the routes")

    by_route = {rid: [] for rid in routes}
    for feature in town["segments_geojson"]["features"]:
        props = feature["properties"]
        rid = props["route_id"]
        if rid not in by_route:
            raise ConfigError(f"segment {props.get('segment_id')} names unknown route '{rid}'")
        coords = [(lat, lon) for lon, lat in feature["geometry"]["coordinates"]]  # GeoJSON is [lon, lat]
        length = sum(distance_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
        mid = (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))
        by_route[rid].append({
            "segment_id": props["segment_id"], "route_id": rid, "order": props["order"],
            "length_m": length, "distance_to_plant_m": distance_m(plant, mid),
            "bearing_from_plant_deg": bearing_deg(plant, mid),
            "road": props.get("road", routes[rid].get("road", "local")),
        })
    for rid, segs in by_route.items():
        if not segs:
            raise ConfigError(f"route '{rid}' has no segments")
        segs.sort(key=lambda s: s["order"])
        if any(s["length_m"] <= 0 for s in segs):
            raise ConfigError(f"route '{rid}' has a zero-length segment")
    town["_segments"] = by_route
    return town


def alert_label(town, model_cfg):
    """The chance label that drives the alert and the school flag: the town file's choice, else the one train.py suggested."""
    label = town.get("alert_label") or model_cfg.get("suggested_label")
    return label if label in model_cfg.get("labels", []) else None


def public_config(town, model_cfg, schema_version):
    """What the dashboard needs to draw a town: the town file plus computed segment facts, bands and model labels."""
    speed = town["travel"]["speed_kmh"]
    out = {
        "schema_version": schema_version, "is_sample": False,
        "town_id": town["town_id"], "name": town["name"], "state": town.get("state"), "timezone": "Asia/Kolkata",
        "has_routes": town["has_routes"], "station": town["station"],
        "school_hours": town["school_hours"], "stale_after_minutes": town["stale_after_minutes"],
        "bands": [{"id": b, "label": label, "pm25_min": lo, "pm25_max": hi} for b, label, lo, hi in BANDS],
        "bands_note": "India AQI breakpoints for PM2.5 in ug/m3. Officially defined on 24-hour averages; used here on hourly values as a guide.",
        "chance_labels": [{"id": name, "pm25_min": model_cfg["label_thresholds"][name]} for name in model_cfg.get("labels", [])],
        "alert_label": alert_label(town, model_cfg), "alert_rule": town["alert_rule"],
        "bad_case": model_cfg.get("bad_case"), "credits": CREDITS,
    }
    if not town["has_routes"]:
        return out
    facts = {s["segment_id"]: s for segs in town["_segments"].values() for s in segs}
    features = []
    for f in town["segments_geojson"]["features"]:
        s = facts[f["properties"]["segment_id"]]
        props = {**f["properties"], "length_m": round(s["length_m"]), "distance_to_plant_m": round(s["distance_to_plant_m"]),
                 "bearing_from_plant_deg": round(s["bearing_from_plant_deg"]), "road": s["road"]}
        features.append({"type": "Feature", "properties": props, "geometry": f["geometry"]})
    routes = []
    for r in town["routes"]:
        segs = town["_segments"][r["route_id"]]
        total = sum(s["length_m"] for s in segs)
        routes.append({**r, "length_m": round(total), "walk_minutes": round(total / 1000 / speed * 60),
                       "segment_ids": [s["segment_id"] for s in segs],
                       "is_usual": r["route_id"] == town["usual_departure"]["route_id"]})
    out.update({
        "plant": town["plant"], "school": town["school"], "home_area": town.get("home_area"),
        "travel": town["travel"], "usual_departure": town["usual_departure"], "routes": routes,
        "segments_geojson": {"type": "FeatureCollection", "features": features},
        "geometry_note": town.get("geometry_note"), "slots": town["slots"], "departure_window": town["departure_window"],
        "exposure_params": town["exposure_params"],
        "exposure_note": "Segment values are estimates from the station forecast and a formula with these settings. They are not measurements.",
    })
    return out
