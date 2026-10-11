"""
SaansSetu: find out whether a live feed can drive the system. Run this once on your own machine.

  python probe_live.py                         weather only (no key needed)
  set OPENAQ_KEY=...   then   python probe_live.py        (Windows; use export on mac/linux)

Get a free OpenAQ key at https://explore.openaq.org/register .

For each town in towns/ it answers four questions and saves every raw answer in probe_out/:
  1. Does live weather (Open-Meteo forecast API) include mixing height?
  2. Does OpenAQ carry this CPCB station, and how far behind real time is it?
  3. Are its values the same raw concentrations as the training archive? (compared hour by hour on past days)
  4. Is each hourly value stamped with the start or the end of its hour, relative to the archive?

Nothing is changed and nothing is sent anywhere except the two requests above. Paste the printed summary back.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from core import ingest
from core.geo import distance_m
from core.townconfig import load_towns

OPENAQ = "https://api.openaq.org/v3"
IST = timezone(timedelta(hours=5, minutes=30))
OUT = Path("probe_out")


def save(name, obj):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def naive_ist(text):
    """'2026-10-06T21:00:00+05:30' or '...Z' -> naive IST timestamp."""
    t = pd.Timestamp(text)
    return t.tz_convert("Asia/Kolkata").tz_localize(None) if t.tzinfo is not None else t


def hours_to_series(results):
    """OpenAQ /hours results -> (values stamped by period start, values stamped by period end), both naive IST."""
    start, end = {}, {}
    for r in results:
        p = r.get("period") or {}
        try:
            a, b = naive_ist(p["datetimeFrom"]["utc"]), naive_ist(p["datetimeTo"]["utc"])
        except (KeyError, TypeError, ValueError):
            continue
        start[a], end[b] = r.get("value"), r.get("value")
    return pd.Series(start, dtype=float).sort_index(), pd.Series(end, dtype=float).sort_index()


def fullest(by_sensor):
    """A location can list the same pollutant twice (an old sensor and its replacement). Given {sensor id: /hours
    results}, returns (sensor id, results) for the one that actually holds the most hourly values."""
    count = lambda results: len(hours_to_series(results)[1])
    best = max(by_sensor, key=lambda sid: count(by_sensor[sid]))
    return best, by_sensor[best]


def compare(archive, by_start, by_end):
    """How well each stamping of the live series matches the archive. Returns one dict per stamping."""
    out = {}
    for label, series in (("hour start", by_start), ("hour end", by_end)):
        both = pd.DataFrame({"archive": archive, "live": series}).dropna()
        if len(both) < 12:
            out[label] = {"hours": len(both), "correlation": None, "median_abs_diff": None, "median_ratio": None}
            continue
        out[label] = {"hours": len(both), "correlation": round(float(both["archive"].corr(both["live"])), 3),
                      "median_abs_diff": round(float((both["archive"] - both["live"]).abs().median()), 2),
                      "median_ratio": round(float((both["live"] / both["archive"]).median()), 3)}
    return out


def verdict(cmp):
    scored = {k: v for k, v in cmp.items() if v["correlation"] is not None}
    if not scored:
        return "NOT ENOUGH OVERLAP to compare with the archive"
    best = max(scored, key=lambda k: scored[k]["correlation"])
    b = scored[best]
    if b["correlation"] < 0.9:
        return f"DOES NOT MATCH the archive (best correlation {b['correlation']}). Do not feed this to the model."
    unit = "same units" if 0.9 <= b["median_ratio"] <= 1.1 else f"DIFFERENT SCALE (live / archive = {b['median_ratio']})"
    return f"MATCHES the archive when stamped by {best} (correlation {b['correlation']}, median gap {b['median_abs_diff']} ug/m3, {unit})"


def probe_weather(town, now):
    feed = ingest.OpenMeteoWeather()
    wx = feed.weather(town["station"], now - pd.Timedelta(hours=30), now)
    save(f"weather_{town['town_id']}.json", json.loads(wx.tail(30).to_json(orient="index", date_format="iso")))
    last = wx.iloc[-1]
    has_blh = "boundary_layer_height" in wx and wx["boundary_layer_height"].notna().any()
    print(f"  weather: latest hour {wx.index[-1]:%Y-%m-%d %H:%M}, wind {last.get('wind_speed_10m')} m/s from {last.get('wind_direction_10m')} deg, "
          f"mixing height {'present: ' + str(last.get('boundary_layer_height')) + ' m' if has_blh else 'NOT OFFERED'}")
    return {"weather_ok": True, "mixing_height": bool(has_blh)}


def probe_openaq(town, key, now, data_dir):
    get = lambda path, **params: ingest.http_json(f"{OPENAQ}{path}", params or None, headers={"X-API-Key": key}, timeout=60)
    st, tid = town["station"], town["town_id"]
    found = get("/locations", coordinates=f"{st['lat']},{st['lon']}", radius=3000, limit=20)
    save(f"openaq_locations_{tid}.json", found)
    here = (st["lat"], st["lon"])
    places = []
    for loc in found.get("results", []):
        c = loc.get("coordinates") or {}
        if c.get("latitude") is None:
            continue
        pm = [s for s in loc.get("sensors", []) if (s.get("parameter") or {}).get("name") == "pm25"]
        places.append({"id": loc["id"], "name": loc.get("name"), "provider": (loc.get("provider") or {}).get("name"),
                       "metres_away": round(distance_m(here, (c["latitude"], c["longitude"]))),
                       "last": ((loc.get("datetimeLast") or {}).get("local")), "pm25_sensors": [x["id"] for x in pm],
                       "sensors": [f"{(s.get('parameter') or {}).get('name')} [{(s.get('parameter') or {}).get('units')}]" for s in loc.get("sensors", [])]})
    places.sort(key=lambda p: p["metres_away"])
    if not places:
        print("  openaq: NO location within 3 km of the station")
        return {"openaq_found": False}
    for p in places[:4]:
        print(f"  openaq: {p['metres_away']:>5} m  id {p['id']}  {p['name']}  ({p['provider']})  last reading {p['last']}")
        print(f"          sensors: {', '.join(p['sensors'])}")
    pick = next((p for p in places if p["pm25_sensors"]), None)
    if pick is None:
        print("  openaq: none of them has a PM2.5 sensor")
        return {"openaq_found": True, "pm25": False}

    utc = lambda t: (pd.Timestamp(t) - pd.Timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hours = lambda sensor, **window: get(f"/sensors/{sensor}/hours", **window).get("results", [])
    recent = {x: hours(x, datetime_from=utc(now - pd.Timedelta(hours=72)), limit=200) for x in pick["pm25_sensors"]}
    save(f"openaq_recent_{tid}.json", recent)
    sensor, results = fullest(recent)
    by_start, by_end = hours_to_series(results)
    out = {"openaq_found": True, "pm25": True, "location_id": pick["id"], "pm25_sensors": pick["pm25_sensors"], "hours_last_72h": len(by_end)}
    if len(by_end):
        lag = int((now - by_end.index[-1]).total_seconds() // 60)
        out["minutes_behind"] = lag
        print(f"  openaq: {len(by_end)} hourly values in the last 72 h. Newest covers {by_start.index[-1]:%H:%M}-{by_end.index[-1]:%H:%M}, "
              f"which is {lag} min behind now. Last six: {[round(v, 1) for v in by_end.tail(6)]}")
    else:
        print("  openaq: NO hourly values in the last 72 hours. This station is not live on OpenAQ.")

    aq_path = Path(data_dir) / f"aq_{st['station_id']}.csv.gz"
    if not aq_path.exists():
        print(f"  archive: {aq_path} not found, so values cannot be compared")
        return out
    archive = pd.read_csv(aq_path, parse_dates=["time"], index_col="time")["pm25"].dropna()
    end = archive.index[-1]
    past = {x: hours(x, datetime_from=utc(end - pd.Timedelta(days=10)), datetime_to=utc(end + pd.Timedelta(hours=2)), limit=400)
            for x in pick["pm25_sensors"]}
    save(f"openaq_past_{tid}.json", past)
    sensor, results = fullest(past)
    cmp = compare(archive.loc[end - pd.Timedelta(days=10):], *hours_to_series(results))
    out["sensor_compared"], out["compare"], out["verdict"] = sensor, cmp, verdict(cmp)
    print(f"  archive vs openaq, {end - pd.Timedelta(days=10):%Y-%m-%d} to {end:%Y-%m-%d}: {cmp}")
    print(f"  -> {out['verdict']}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Check the live feeds for every town.")
    ap.add_argument("--towns", default="towns")
    ap.add_argument("--data", default="saanssetu_data")
    args = ap.parse_args()
    towns, problems = load_towns(args.towns)
    if not towns:
        sys.exit(f"No usable town files in {args.towns}/ {problems or ''}")
    key = os.environ.get("OPENAQ_KEY")
    now = pd.Timestamp(datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0))
    print(f"Now (IST): {now:%Y-%m-%d %H:%M}.  OpenAQ key: {'set' if key else 'NOT SET, so only weather is checked'}\n")
    summary = {}
    for town_id, town in towns.items():
        print(f"{town['name']}  ({town['station']['station_id']}, {town['station'].get('lat')}, {town['station'].get('lon')})")
        s = {}
        for name, step in (("weather", lambda: probe_weather(town, now)), ("openaq", lambda: probe_openaq(town, key, now, args.data))):
            if name == "openaq" and not key:
                continue
            try:
                s.update(step())
            except Exception as e:
                s[f"{name}_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"  {name}: FAILED ({s[f'{name}_error']})")
        summary[town_id] = s
        print()
    save("summary.json", summary)
    print("=== Summary (paste this back) ===")
    print(json.dumps(summary, indent=1, default=str))
    print(f"\nRaw answers are in {OUT}/")


if __name__ == "__main__":
    main()
