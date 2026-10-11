"""
SaansSetu: put real routes into a town file.

  1. Open https://geojson.io and find the town.
  2. Draw each walking route as one line, from the home area to the school gate, following the real roads.
     Draw the usual route first.
  3. Drop three markers and give each a property called  role  with the value  school,  plant  or  home.
     (Click the marker, add a row: role | school.) Optional on any feature: name, name_hi. On a line: road = main or local.
  4. Save > GeoJSON, then:

  python make_routes.py towns/asansol.json map.geojson

Each line is cut into pieces of about 100 m (--segment). The town file is rewritten with the real geometry;
the old one is kept as <name>.bak. Nothing is fetched from the internet.
"""
import argparse
import copy
import json
import re
import shutil
import sys
from pathlib import Path

from core.geo import distance_m, minutes
from core.townconfig import ConfigError, prepare


def cut(line, piece_m):
    """Cuts a [lon, lat] polyline into pieces of roughly piece_m, keeping every bend. Returns a list of polylines."""
    pts = [tuple(p[:2]) for p in line]
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    if len(pts) < 2:
        raise ValueError("a route needs at least two different points")
    dist = lambda a, b: distance_m((a[1], a[0]), (b[1], b[0]))
    total = sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    n = max(1, round(total / piece_m))
    target = total / n                      # equal pieces, so there is no short stub at the end
    pieces, current, walked = [], [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        leg, done = dist(a, b), 0.0
        while len(pieces) < n - 1 and walked + (leg - done) >= target - 1e-9:
            done += target - walked
            f = done / leg
            split = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
            current.append(split)
            pieces.append(current)
            current, walked = [split], 0.0
        walked += leg - done
        current.append(b)
    pieces.append(current)
    return [[[round(x, 6), round(y, 6)] for x, y in piece] for piece in pieces], total


def build(raw, drawn, piece_m=100):
    """raw: the town file as written. Returns (new town file, list of notes).
    Raises ValueError with a plain message when the drawing cannot be used."""
    town = prepare(copy.deepcopy(raw))      # for the walking speed and other defaults
    feats = drawn.get("features", [])
    lines = [f for f in feats if f["geometry"]["type"] == "LineString"]
    points = {str((f.get("properties") or {}).get("role", "")).lower(): f for f in feats if f["geometry"]["type"] == "Point"}
    if len(lines) < 2:
        raise ValueError(f"found {len(lines)} line(s); draw at least two routes so there is something to compare")
    notes, new = [], copy.deepcopy(raw)
    for role in ("school", "plant", "home"):
        if role not in points:
            if role == "home":
                continue
            raise ValueError(f"no marker with role = {role}")
        lon, lat = points[role]["geometry"]["coordinates"][:2]
        props = points[role].get("properties") or {}
        key = "home_area" if role == "home" else role
        old = {k: v for k, v in (raw.get(key) or {}).items() if k not in ("placeholder", "approximate", "lat", "lon")}
        new[key] = {**old, **({"name": props["name"]} if props.get("name") else {}), "lat": round(lat, 6), "lon": round(lon, 6)}
    new["school"].setdefault("school_id", "school-1")
    new["school"].setdefault("name", "School")
    new["school"].setdefault("starts_at", "08:00")
    new["plant"].setdefault("name", "Plant area")

    school = (new["school"]["lat"], new["school"]["lon"])
    routes, features, used = [], [], set()
    for k, f in enumerate(lines):
        props = f.get("properties") or {}
        name = props.get("name") or f"Route {chr(65 + k)}"
        rid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"route-{k + 1}"
        if rid in used:
            rid = f"{rid}-{k + 1}"
        used.add(rid)
        coords = f["geometry"]["coordinates"]
        ends = [distance_m(school, (coords[0][1], coords[0][0])), distance_m(school, (coords[-1][1], coords[-1][0]))]
        if ends[0] < ends[1]:               # drawn from the school outward: turn it round
            coords = coords[::-1]
            notes.append(f"{name}: drawn from the school end, so it was reversed to run home -> school")
        if min(ends) > 150:
            notes.append(f"{name}: ends {round(min(ends))} m from the school marker. Check the line reaches the gate.")
        pieces, total = cut(coords, piece_m)
        road = props.get("road", "local")
        if "road" not in props:
            notes.append(f"{name}: no road type given, so it counts as a local road. Set road = main on a busy road.")
        routes.append({"route_id": rid, "name": name, "name_hi": props.get("name_hi", name), "road": road, "is_usual": k == 0})
        for i, piece in enumerate(pieces):
            features.append({"type": "Feature", "properties": {"segment_id": f"{rid}-{i + 1:02d}", "route_id": rid, "order": i + 1},
                             "geometry": {"type": "LineString", "coordinates": piece}})
        walk = total / 1000 / town["travel"]["speed_kmh"] * 60
        notes.append(f"{name}: {round(total)} m, {len(pieces)} segments, about {round(walk)} min on foot, road type {road}")
        if "name_hi" not in props:
            notes.append(f"{name}: no name_hi given, so the Hindi alert will use the English name")
    starts = [f["geometry"]["coordinates"][0] for f in features if f["properties"]["order"] == 1]
    spread = max(distance_m((a[1], a[0]), (b[1], b[0])) for a in starts for b in starts)
    if spread > 200:
        notes.append(f"The routes start {round(spread)} m apart. They should start from the same home area.")

    new["routes"], new["segments_geojson"] = routes, {"type": "FeatureCollection", "features": features}
    leave = (raw.get("usual_departure") or {}).get("leave", "07:20")
    new["usual_departure"] = {"route_id": routes[0]["route_id"], "leave": leave}
    new["geometry_note"] = drawn.get("geometry_note") or "Routes traced by hand on OpenStreetMap."
    check = prepare(copy.deepcopy(new))     # the new file must load cleanly
    start, win = minutes(check["school"]["starts_at"]), check["departure_window"]
    for r in routes:
        walk = sum(s["length_m"] for s in check["_segments"][r["route_id"]]) / 1000 / check["travel"]["speed_kmh"] * 60
        if minutes(win["earliest"]) + walk > start:
            notes.append(f"{r['name']}: even leaving at {win['earliest']}, a {round(walk)} min walk arrives after school starts "
                         f"({check['school']['starts_at']}). Move departure_window earlier in the town file.")
        if r["is_usual"] and minutes(leave) + walk > start:
            notes.append(f"{r['name']}: the usual leaving time {leave} arrives after school starts. Change usual_departure.leave.")
    return new, notes


def main():
    ap = argparse.ArgumentParser(description="Put routes drawn on geojson.io into a town file.")
    ap.add_argument("town", help="town file to update, e.g. towns/asansol.json")
    ap.add_argument("drawing", help="GeoJSON saved from geojson.io")
    ap.add_argument("--segment", type=float, default=100, help="segment length in metres (default 100)")
    args = ap.parse_args()
    path = Path(args.town)
    try:
        new, notes = build(json.loads(path.read_text(encoding="utf-8")), json.loads(Path(args.drawing).read_text(encoding="utf-8")), args.segment)
    except (ValueError, ConfigError, KeyError) as e:
        sys.exit(f"Cannot use this drawing: {e}")
    shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {path} (old version kept as {path.name}.bak)\n")
    for n in notes:
        print(f"  - {n}")
    print(f"\nCheck it:  python replay.py --town {path} --suggest")


if __name__ == "__main__":
    main()
