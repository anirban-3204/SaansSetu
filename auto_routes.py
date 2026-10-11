"""
SaansSetu: real routes without tracing them by hand.

You give a few points. A free OpenStreetMap routing service works out the walking path along real roads
between them, and this script writes map.geojson in the form make_routes.py expects.

  1. python auto_routes.py rourkela             writes route_points_rourkela.json for you to fill in
  2. Fill it in. Every "at" and "via" is "lat, lon". In Google Maps: right-click the spot, then click the
     numbers at the top of the menu to copy them.
       school   the school gate
       plant    the middle of the plant area
       home     where the walk starts
       routes   the usual route first. "via" is one or more points ON the road that route should use,
                in walking order. "road" is main or local. "name_hi" is optional.
  3. python auto_routes.py rourkela             the same command again: asks the routing service, writes
                                                map_rourkela.geojson and prints a check
  4. Drag map_rourkela.geojson onto https://geojson.io to look at it. If a route is wrong, move a via point and repeat 3.
  5. python make_routes.py towns/rourkela.json map_rourkela.geojson

Any town that has a file in towns/ works the same way; each town keeps its own points file and its own map file.
A forecast-only town becomes a town with routes at step 5.

The first version of this script, for Asansol only, still works as it did:
  python auto_routes.py                         writes route_points.json
  python auto_routes.py route_points.json       writes map.geojson for towns/asansol.json

Step 3 needs the internet. Routes come from the OSRM walking service that FOSSGIS runs on OpenStreetMap data.
It is a shared free service: run this a few times, not in a loop.
"""
import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from core.geo import bearing_deg, compass, distance_m, minutes

SERVER = "https://routing.openstreetmap.de/routed-foot"
NOTE = ("Routes follow OpenStreetMap roads: walking paths from the OSRM routing service (FOSSGIS) "
        "between hand-picked points.")
EMPTY = "PASTE lat, lon"
TEMPLATE = {
    "how_to_fill": [
        "Every 'at' and 'via' is 'lat, lon'. In Google Maps: right-click the spot, click the numbers to copy.",
        "The first route is the usual one. 'via' is one or more points ON the road that route uses, in walking order.",
        "'road' is main or local. 'name' is the road name a parent would recognise. 'name_hi' is optional.",
    ],
    "school": {"name": "", "at": EMPTY},
    "plant": {"at": EMPTY},
    "home": {"name": "", "at": EMPTY},
    "routes": [
        {"name": "", "name_hi": "", "road": "main", "via": [EMPTY]},
        {"name": "", "name_hi": "", "road": "local", "via": [EMPTY]},
    ],
}
ROADS = ("main", "local", "footpath")
FAR_FROM_STATION_KM = 10


def blank_for(town_id, town):
    """The file to fill in. For a named town it carries the town id and says where the air station is."""
    doc = json.loads(json.dumps(TEMPLATE))
    station = (town or {}).get("station") or {}
    if "lat" in station and "lon" in station:
        doc["how_to_fill"].append(f"The air station for {town.get('name', town_id)} is at {station['lat']}, {station['lon']} "
                                  f"({station.get('name', 'station')}). Pick a school within a few km of it: the forecast is for the air there.")
    return {"town": town_id, **doc} if town_id else doc
WINDS = [(0, "N"), (45, "NE"), (90, "E"), (135, "SE"), (180, "S"), (225, "SW"), (270, "W"), (315, "NW")]


# ---------------------------------------------------------------- reading the points file

def parse_point(text, what):
    """'23.6853, 86.9460' (lat, lon, the order Google Maps copies) -> (lat, lon)."""
    if EMPTY in str(text):
        raise ValueError(f"{what} is still '{EMPTY}'. Put the real point there.")
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(text))
    if len(nums) != 2:
        raise ValueError(f"{what}: expected 'lat, lon' such as '23.6853, 86.9460', got {text!r}")
    lat, lon = float(nums[0]), float(nums[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"{what}: {text!r} is not a lat, lon pair. The latitude comes first.")
    return lat, lon


def read_spec(doc):
    """The points file as plain values. Raises ValueError with a plain message."""
    for key in ("school", "plant", "home", "routes"):
        if key not in doc:
            raise ValueError(f"the points file has no '{key}'")
    spec = {role: {"name": str(doc[role].get("name") or "").strip(), "at": parse_point(doc[role].get("at", ""), role)}
            for role in ("school", "plant", "home")}
    if len(doc["routes"]) < 2:
        raise ValueError("give at least two routes, so there is something to compare")
    spec["routes"] = []
    for k, r in enumerate(doc["routes"]):
        what = f"route {k + 1}"
        via = r.get("via") or []
        if isinstance(via, str):
            via = [via]
        road = str(r.get("road") or "local").strip().lower()
        if road not in ROADS:
            raise ValueError(f"{what}: road is {road!r}; use main or local")
        spec["routes"].append({"name": str(r.get("name") or "").strip(), "name_hi": str(r.get("name_hi") or "").strip(),
                               "road": road, "via": [parse_point(v, f"{what} via {i + 1}") for i, v in enumerate(via)]})
    if distance_m(spec["home"]["at"], spec["school"]["at"]) < 150:
        raise ValueError("home and school are less than 150 m apart; there is no walk to compare")
    return spec


def check_near(spec, centre, town_name):
    """Catches a point pasted as lon, lat, or a point from the wrong town."""
    points = [(role, spec[role]["at"]) for role in ("school", "plant", "home")]
    points += [(f"route {k + 1} via {i + 1}", p) for k, r in enumerate(spec["routes"]) for i, p in enumerate(r["via"])]
    for what, (lat, lon) in points:
        far = distance_m(centre, (lat, lon)) / 1000
        if far <= 30:
            continue
        if -90 <= lon <= 90 and distance_m(centre, (lon, lat)) / 1000 <= 30:
            raise ValueError(f"{what}: {lat}, {lon} looks like lon, lat. Swap the two numbers (latitude first).")
        raise ValueError(f"{what}: {lat}, {lon} is {round(far)} km from the {town_name} station. Check the paste.")


# ---------------------------------------------------------------- the routing service

def ask(url, tries=3):
    """One JSON answer from the routing service. A refused request (HTTP 400) comes back as its JSON explanation."""
    request = urllib.request.Request(url, headers={"User-Agent": "SaansSetu-auto-routes/1.0 (student hackathon project)"})
    last = "no answer"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:
                return json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 400:
                try:
                    return json.loads(e.read().decode("utf-8", "replace"))
                except ValueError:
                    pass
            last = f"HTTP {e.code}"
        except (OSError, ValueError) as e:      # no connection, timeout, or an answer that is not JSON
            last = str(getattr(e, "reason", e))
        if attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"the routing service did not answer ({last})")


def walk(points, server):
    """points: [(lat, lon), ...] in walking order. Returns (line as [[lon, lat], ...], the service's snapped waypoints)."""
    path = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    data = ask(f"{server}/route/v1/foot/{path}?overview=full&geometries=geojson")
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"no walking route found ({data.get('code')}: {data.get('message', 'no reason given')})")
    line = [[round(x, 6), round(y, 6)] for x, y in data["routes"][0]["geometry"]["coordinates"]]
    line = [p for i, p in enumerate(line) if i == 0 or p != line[i - 1]]
    if len(line) < 2:
        raise RuntimeError("the routing service returned an empty route")
    return line, data.get("waypoints") or []


# ---------------------------------------------------------------- measuring a line

def edges(line):
    """[(edge key, length in m), ...]. The key is the same whichever way the edge is walked."""
    return [(frozenset((tuple(a), tuple(b))), distance_m((a[1], a[0]), (b[1], b[0]))) for a, b in zip(line, line[1:])]


def stretches(line, step=50.0):
    """The line as short stretches: [(midpoint (lat, lon), length in m), ...]."""
    out = []
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        d = distance_m((y0, x0), (y1, x1))
        n = max(1, math.ceil(d / step))
        out += [((y0 + (i + 0.5) / n * (y1 - y0), x0 + (i + 0.5) / n * (x1 - x0)), d / n) for i in range(n)]
    return out


def walked_twice_m(line):
    """Metres of road this line walks more than once: the sign of an out-and-back spur to a via point."""
    seen, twice = set(), 0.0
    for key, d in edges(line):
        if key in seen:
            twice += d
        seen.add(key)
    return twice


def shared_share(line, other):
    """Share of this line's length that runs on the same road pieces as the other line."""
    theirs = {key for key, _ in edges(other)}
    mine = edges(line)
    return sum(d for key, d in mine if key in theirs) / sum(d for _, d in mine)


def plume_by_wind(lines, plant, params):
    """{wind_from_deg: [length-weighted plume add-on per line]}, from the backend's own formula."""
    from core.exposure import plume_factor
    table = {}
    for wind, _ in WINDS:
        row = []
        for line in lines:
            parts = stretches(line)
            add = sum(plume_factor({"bearing_from_plant_deg": bearing_deg(plant, mid), "distance_to_plant_m": distance_m(plant, mid)},
                                   wind, params) * d for mid, d in parts)
            row.append(add / sum(d for _, d in parts))
        table[wind] = row
    return table


# ---------------------------------------------------------------- the work

def build(spec, server, town=None, pause=1.0):
    """Returns (map.geojson as a dict, lines to print, number of warnings). Raises RuntimeError when a route cannot be found."""
    town = town or {}
    speed = (town.get("travel") or {}).get("speed_kmh", 4.5)
    starts = (town.get("school") or {}).get("starts_at", "08:00")
    leave = (town.get("usual_departure") or {}).get("leave", "07:20")
    earliest = (town.get("departure_window") or {}).get("earliest", "07:00")
    plant, said, warned = spec["plant"]["at"], [], 0

    def warn(text):
        nonlocal warned
        warned += 1
        said.append(f"  ! {text}")

    features, lines, names = [], [], []
    for role in ("school", "plant", "home"):
        lat, lon = spec[role]["at"]
        props = {"role": role, **({"name": spec[role]["name"]} if spec[role]["name"] else {})}
        features.append({"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    if not spec["school"]["name"]:
        warn("school has no name, so the name already in the town file stays")
    station = town.get("station") or {}
    if "lat" in station and "lon" in station:
        km = distance_m((station["lat"], station["lon"]), spec["school"]["at"]) / 1000
        said.append(f"The school is {km:.1f} km from the air station ({station.get('name', 'station')}).")
        if km > FAR_FROM_STATION_KM:
            warn(f"the forecast is for the air at the station, {round(km)} km away. Pick a school nearer the station.")

    for k, r in enumerate(spec["routes"]):
        if k:
            time.sleep(pause)               # be polite to a shared service
        line, snapped = walk([spec["home"]["at"], *r["via"], spec["school"]["at"]], server)
        via_names = [w.get("name") for w in snapped[1:-1] if w.get("name")]
        name = r["name"] or (via_names[0] if via_names else f"Route {chr(65 + k)}")
        while name in names:
            name = f"{name} ({k + 1})"
        names.append(name)
        lines.append(line)
        props = {"name": name, "road": r["road"], **({"name_hi": r["name_hi"]} if r["name_hi"] else {})}
        features.append({"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": line}})

        total = sum(d for _, d in edges(line))
        took = total / 1000 / speed * 60
        said.append(f"\n{name}" + ("  (the usual route)" if k == 0 else ""))
        if not r["name"]:
            said.append(f"  no name given, so it is called '{name}'" + (" after the road at its first via point" if via_names else ""))
        said.append(f"  {round(total)} m, about {round(took)} min on foot at {speed} km/h, road type {r['road']}")
        start_gap = distance_m(spec["home"]["at"], (line[0][1], line[0][0]))
        end_gap = distance_m(spec["school"]["at"], (line[-1][1], line[-1][0]))
        said.append(f"  starts {round(start_gap)} m from your home point, ends {round(end_gap)} m from your school point")
        if start_gap > 100:
            warn(f"the nearest road is {round(start_gap)} m from the home point. Move the home point onto a road.")
        if end_gap > 150:
            warn(f"the route ends {round(end_gap)} m from the school point; make_routes.py will say it does not reach the gate. "
                 f"Move the school point to the gate on the road.")
        for i, w in enumerate(snapped[1:-1]):
            off = w.get("distance") or 0
            said.append(f"  via {i + 1} is {round(off)} m from the nearest road" + (f" ('{w['name']}')" if w.get("name") else ""))
            if off > 50:
                warn(f"via {i + 1} is not on a road the service knows. The route may not use the road you meant.")
        if not r["via"]:
            said.append("  no via point: this is simply the shortest walk")
        twice = walked_twice_m(line)
        if twice > 30:
            warn(f"this route walks {round(twice)} m of road twice (out and back). A via point is probably on a side street; "
                 f"move it onto the through road.")
        near, mid = min((distance_m(plant, m), m) for m, _ in stretches(line))
        said.append(f"  closest to the plant: {round(near)} m, where the route is {compass(bearing_deg(plant, mid))} of it")
        if minutes(earliest) + took > minutes(starts):
            warn(f"even leaving at {earliest}, a {round(took)} min walk arrives after school starts ({starts}). Pick a closer home point.")
        elif k == 0 and minutes(leave) + took > minutes(starts):
            warn(f"leaving at the usual {leave}, this arrives after school starts ({starts}). "
                 f"Pick a closer home point or change usual_departure.leave in the town file.")

    said.append("")
    for k in range(1, len(lines)):
        share = shared_share(lines[k], lines[0])
        said.append(f"{names[k]} shares {round(share * 100)}% of its length with {names[0]}.")
        if share > 0.6:
            warn("these are mostly the same walk. Move a via point so the routes separate earlier.")

    try:
        from core.townconfig import DEFAULTS
        params = {**DEFAULTS["exposure_params"], **(town.get("exposure_params") or {})}
        table = plume_by_wind(lines, plant, params)
    except Exception as e:                  # the check is a convenience; the map file does not depend on it
        said.append(f"(plant check skipped: {type(e).__name__}: {e})")
        table = None
    if table:
        said.append("\nWhat the plant adds to each route, by wind direction (the backend's own formula, before traffic):")
        said.append("  wind from  " + "".join(f"{n[:22]:>24}" for n in names))
        for wind, label in WINDS:
            said.append(f"  {label:<9}  " + "".join(f"{'+' + str(round(v * 100)) + '%':>24}" for v in table[wind]))
        gap, wind = max((max(row) - min(row), wind) for wind, row in table.items())
        if gap < 0.10:
            warn("the plant makes almost no difference between these routes in any wind (under 10 points). "
                 "Take one route closer to the plant, or the other further from it.")
        else:
            worse = names[table[wind].index(max(table[wind]))]
            said.append(f"  Biggest gap: {round(gap * 100)} points with wind from {dict(WINDS)[wind]}, where {worse} is the worse one.")
            if all(row[0] <= min(row[1:]) + 0.02 for row in table.values()):
                warn(f"the usual route ({names[0]}) is never the worse one, so the alert will never suggest a change of route. "
                     f"Put the plant-side route first.")
        said.append("  This is a rough guide. The real test is replay.py --suggest after make_routes.py.")

    return {"type": "FeatureCollection", "geometry_note": NOTE, "features": features}, said, warned


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")    # a Hindi name must not crash a Windows console
    ap = argparse.ArgumentParser(description="Work out real walking routes from a few points and write map.geojson.")
    ap.add_argument("what", nargs="?", help="a town, e.g. rourkela (uses towns/rourkela.json), or a filled-in points file. "
                                            "Leave it out for a blank route_points.json for Asansol")
    ap.add_argument("--town", default=None, help="town file to check against (default: towns/<town>.json)")
    ap.add_argument("--out", default=None, help="where to write the result (default: map_<town>.geojson)")
    ap.add_argument("--server", default=SERVER, help="OSRM walking server to ask")
    args = ap.parse_args()

    town_id, points = None, Path(args.what) if args.what else None
    if args.what and not args.what.lower().endswith(".json"):          # a town name
        town_id = args.what.strip().lower()
        points = Path(f"route_points_{town_id}.json")
        if not Path(args.town or f"towns/{town_id}.json").exists():
            here = ", ".join(sorted(p.stem for p in Path("towns").glob("*.json"))) or "none"
            sys.exit(f"There is no towns/{town_id}.json. Towns here: {here}.")
    if points is None or (town_id and not points.exists()):            # nothing to read yet: write the blank file
        blank = points or Path("route_points.json")
        if blank.exists():
            sys.exit(f"{blank} is already here. Fill it in, then:  python auto_routes.py {blank}")
        town_path = Path(args.town or f"towns/{town_id or 'asansol'}.json")
        try:
            known = json.loads(town_path.read_text(encoding="utf-8")) if town_path.exists() else {}
        except ValueError as e:
            sys.exit(f"{town_path} is not valid JSON: {e}")
        blank.write_text(json.dumps(blank_for(town_id, known), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {blank}. Fill in the points, then:  python auto_routes.py {town_id or blank}")
        return

    town = {}
    try:
        doc = json.loads(points.read_text(encoding="utf-8-sig"))
        town_id = town_id or str(doc.get("town") or "").strip().lower() or None
        town_path = Path(args.town or f"towns/{town_id or 'asansol'}.json")
        out = args.out or (f"map_{town_id}.geojson" if town_id else "map.geojson")
        if town_id and not town_path.exists():
            raise ValueError(f"it names the town '{town_id}', but there is no {town_path.as_posix()}")
        spec = read_spec(doc)
        if town_path.exists():
            town = json.loads(town_path.read_text(encoding="utf-8"))
            station = town.get("station") or {}
            if "lat" in station and "lon" in station:
                check_near(spec, (station["lat"], station["lon"]), town.get("name", "town"))
    except json.JSONDecodeError as e:
        sys.exit(f"Not valid JSON at line {e.lineno} ({e.msg}). A missing comma or quote is the usual cause.")
    except (OSError, ValueError, AttributeError, TypeError) as e:
        sys.exit(f"Cannot use {points}: {e}")
    if town:
        print(f"{town.get('name', town_id)}: points from {points}, checked against {town_path.as_posix()}")
    else:
        print(f"({town_path.as_posix()} not found, so the points were not checked against the town)")

    try:
        drawing, said, warned = build(spec, args.server.rstrip("/"), town)
    except RuntimeError as e:
        sys.exit(f"Could not get the routes: {e}\nNothing was written. Check the internet connection and the points, then run it again.")
    Path(out).write_text(json.dumps(drawing, ensure_ascii=False) + "\n", encoding="utf-8")
    for line in said:
        print(line)
    print(f"\nWrote {out}: {len(spec['routes'])} routes and 3 markers, {warned} warning(s) marked with '!'.")
    print(f"Look at it:  drag {out} onto https://geojson.io")
    print(f"Then:        python make_routes.py {town_path.as_posix()} {out}")


if __name__ == "__main__":
    main()
