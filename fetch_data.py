"""
SaansSetu: fetch station + weather history for steel-town CPCB stations.

What it does
  1. Finds every CPCB continuous station in the candidate towns (XKDR India Air Quality API).
  2. Pulls hourly PM2.5, PM10, NO2, CO, SO2 from Oct 2019 to Sep 2025.
  3. Pulls hourly weather for each station area (Open-Meteo archive, no key needed).
  4. Prints a scouting table (winter data completeness, morning severity) and zips everything.

Run it
  pip install requests pandas
  python fetch_data.py aqi_yourkey
  python fetch_data.py aqi_yourkey --cities Bhilai Asansol Visakhapatnam     (optional: fewer towns)

Get a free key in about a minute at https://airquality.xkdr.org/signup (the demo key only reads 2024).
Safe to re-run: finished files are kept and skipped, so a failed run picks up where it stopped.
(Weather is fetched a quarter at a time and kept in saanssetu_cache/; delete that folder when done.)
Output: saanssetu_data.zip

Data credit: India Air Quality Database, XKDR Forum (CC BY 4.0), from CPCB's CAAQM network.
Weather: Open-Meteo historical weather API (CC BY 4.0).
"""

import argparse
import io
import os
import shutil
import sys
import time
from pathlib import Path

import pandas as pd
import requests

XKDR = "https://airquality.xkdr.org"
METEO = "https://archive-api.open-meteo.com/v1/archive"
UA = {"User-Agent": "saanssetu-fetch/1.0"}

# Steel / heavy-industry towns. Jamshedpur is listed only to confirm whether it has a
# continuous station at all.
CITIES = ["Bhilai", "Asansol", "Visakhapatnam", "Rourkela", "Durgapur", "Angul", "Jamshedpur"]
POLLUTANTS = ["PM2.5", "PM10", "NO2", "CO", "SO2"]
RENAME = {"PM2.5": "pm25", "PM10": "pm10", "NO2": "no2", "CO": "co", "SO2": "so2"}
START, END = "2019-10-01", "2025-09-30"
WX_VARS = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "wind_speed_10m",
           "wind_direction_10m", "precipitation", "surface_pressure", "cloud_cover",
           "boundary_layer_height"]
WX_PAUSE = 1.5  # seconds between weather requests
WX_TRIES = 5    # the weather archive sometimes times out mid-response; each quarter is retried

OUT = Path("saanssetu_data")
CACHE = Path("saanssetu_cache")  # finished weather quarters, so a re-run only fetches what is missing


def get(url, params, headers=None, tries=5):
    headers = {**UA, **(headers or {})}
    r = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=120)
        except requests.RequestException as e:
            if attempt == tries:
                raise
            print(f"    network error ({e.__class__.__name__}), retrying...")
            time.sleep(5 * attempt)
            continue
        if r.status_code == 401:
            sys.exit("XKDR rejected the key. Check it, or get a new one at https://airquality.xkdr.org/signup")
        if r.status_code == 429 and attempt < tries:
            wait = int(r.headers.get("Retry-After", 65))
            print(f"    rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < tries:
            time.sleep(5 * attempt)
            continue
        return r
    return r


def xkdr_csv(path, params, key):
    """Returns (frame, truncated, ok)."""
    r = get(f"{XKDR}{path}", params, headers={"Authorization": f"Bearer {key}"})
    time.sleep(0.2)
    if r.status_code != 200:
        print(f"    XKDR {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return pd.DataFrame(), False, False
    truncated = r.headers.get("X-Truncated", "false").lower() == "true"
    if not r.text.strip():
        return pd.DataFrame(), truncated, True
    return pd.read_csv(io.StringIO(r.text)), truncated, True


def year_ranges():
    s, e = pd.Timestamp(START), pd.Timestamp(END)
    for y in range(s.year, e.year + 1):
        a = max(s, pd.Timestamp(f"{y}-01-01"))
        b = min(e, pd.Timestamp(f"{y}-12-31"))
        yield y, a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")


def month_ranges(start, end):
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    m = s.to_period("M")
    while m.start_time <= e:
        a = max(m.start_time, s)
        b = min(m.end_time.normalize(), e)
        yield a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")
        m += 1


def fetch_station(sid, key):
    """Returns (wide hourly frame, list of years that failed)."""
    frames, failed = [], []
    for y, a, b in year_ranges():
        base = [("station", sid), ("agg", "raw"), ("format", "csv")] + [("parameter", p) for p in POLLUTANTS]
        df, trunc, ok = xkdr_csv("/v1/measurements", base + [("start", a), ("end", b)], key)
        if ok and trunc:  # row cap hit (demo key): fall back to monthly chunks
            parts = []
            for ma, mb in month_ranges(a, b):
                part, t2, ok2 = xkdr_csv("/v1/measurements", base + [("start", ma), ("end", mb)], key)
                ok = ok and ok2 and not t2
                parts.append(part)
            df = pd.concat(parts, ignore_index=True)
        if not ok:
            failed.append(y)
            print(f"    {y}: FAILED")
            continue
        if len(df):
            frames.append(df)
        print(f"    {y}: {len(df):,} rows")
    if not frames:
        return pd.DataFrame(), failed
    long = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(long["collected_at"])
    if ts.dt.tz is not None:  # the API serves naive IST; guard in case that ever changes
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    long["collected_at"] = ts
    wide = long.pivot_table(index="collected_at", columns="parameter_name", values="value", aggfunc="mean")
    wide = wide.rename(columns=RENAME)
    wide.columns.name = None
    wide.index.name = "time"
    return wide.sort_index(), failed


def quarter_ranges():
    s, e = pd.Timestamp(START), pd.Timestamp(END)
    q = s.to_period("Q")
    while q.start_time <= e:
        a = max(q.start_time, s)
        b = min(q.end_time.normalize(), e)
        yield str(q), a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")
        q += 1


def fetch_weather(lat, lon):
    """Returns (hourly frame, list of quarters that failed). Fetched in quarters, each retried and cached."""
    CACHE.mkdir(exist_ok=True)
    frames, failed = [], []
    vars_ = list(WX_VARS)
    for label, a, b in quarter_ranges():
        part = CACHE / f"wx_{lat:.1f}_{lon:.1f}_{label}.csv"
        if part.exists():
            frames.append(pd.read_csv(part))
            continue
        df, why = None, ""
        for attempt in range(1, WX_TRIES + 1):
            params = {"latitude": lat, "longitude": lon, "start_date": a, "end_date": b,
                      "hourly": ",".join(vars_), "timezone": "Asia/Kolkata", "wind_speed_unit": "ms"}
            r = get(METEO, params)
            if r.status_code == 400 and "boundary_layer_height" in vars_:
                print("    mixing height (boundary_layer_height) not offered, continuing without it")
                vars_.remove("boundary_layer_height")
                continue
            try:
                js = r.json()
            except ValueError:
                js = {}
            hourly = js.get("hourly") if isinstance(js, dict) else None
            if r.status_code == 200 and hourly and len(hourly.get("time", [])) > 0:
                df = pd.DataFrame(hourly)
                break
            why = f"HTTP {r.status_code}: {r.text[:120]}"
            if attempt < WX_TRIES:
                time.sleep(10 * attempt)
        if df is None:
            print(f"    weather {label}: FAILED ({why})")
            failed.append(label)
        else:
            df.to_csv(part, index=False)
            frames.append(df)
            print(f"    weather {label}: ok")
        time.sleep(WX_PAUSE)
    if failed or not frames:
        return pd.DataFrame(), failed
    wx = pd.concat(frames, ignore_index=True)
    wx["time"] = pd.to_datetime(wx["time"])
    wx = wx.drop_duplicates("time").set_index("time").sort_index()
    empty = [c for c in wx.columns if wx[c].isna().all()]
    if empty:
        print(f"    note: no values returned for {', '.join(empty)}")
    return wx, failed


def scout(sid, name, city, aq):
    row = {"station_id": sid, "station": name, "city": city, "first": None, "last": None, "good_winters": 0}
    if aq.empty or "pm25" not in aq or aq["pm25"].notna().sum() == 0:
        return row
    pm = aq["pm25"]
    row["first"] = str(pm.first_valid_index().date())
    row["last"] = str(pm.last_valid_index().date())
    good = 0
    for s in range(2019, 2025):
        idx = pd.date_range(f"{s}-10-01", f"{s + 1}-02-28 23:00", freq="h")
        comp = pm.reindex(idx).notna().mean()
        row[f"winter_{s}_{str(s + 1)[2:]}"] = round(float(comp), 2)
        good += comp >= 0.75
    row["good_winters"] = int(good)
    win = pm[pm.index.month.isin([10, 11, 12, 1, 2])].dropna()
    morn = win[win.index.hour.isin([7, 8])]
    eight = win[win.index.hour == 8]
    row["winter_7to9am_median_pm25"] = round(float(morn.median()), 1) if len(morn) else None
    row["share_8am_poor"] = round(float((eight >= 91).mean()), 2) if len(eight) else None
    return row


def read_saved(path):
    return pd.read_csv(path, parse_dates=["time"], index_col="time")


def main():
    ap = argparse.ArgumentParser(description="Fetch SaansSetu training data.")
    ap.add_argument("key", nargs="?", default=os.environ.get("XKDR_KEY"), help="XKDR API key (or set XKDR_KEY)")
    ap.add_argument("--cities", nargs="+", default=CITIES)
    args = ap.parse_args()
    if not args.key:
        sys.exit("Pass your XKDR key: python fetch_data.py aqi_...   (free key: https://airquality.xkdr.org/signup)")
    key = args.key
    OUT.mkdir(exist_ok=True)
    t0 = time.time()

    print("Finding stations...")
    found = []
    for city in args.cities:
        df, _, ok = xkdr_csv("/v1/stations", {"city": city, "format": "csv"}, key)
        if "source" in df:
            df = df[df["source"] == "cpcb_caaqm"]
        if df.empty:
            print(f"  {city}: {'no continuous station' if ok else 'lookup failed'}")
            continue
        print(f"  {city}: {len(df)} station(s)")
        found.append(df)
    if not found:
        sys.exit("No stations found. Check the key and your connection.")
    stations = pd.concat(found, ignore_index=True)

    def wx_name(s):
        if pd.isna(s.get("latitude")) or pd.isna(s.get("longitude")):
            return None
        return f"wx_{round(float(s['latitude']), 1):.1f}_{round(float(s['longitude']), 1):.1f}.csv.gz"

    stations["wx_file"] = [wx_name(s) for _, s in stations.iterrows()]
    stations.to_csv(OUT / "stations.csv", index=False)

    rows, problems, notes = [], [], []
    for _, s in stations.iterrows():
        sid, name, city = s["station_id"], s.get("station_name", s["station_id"]), s.get("city_name", "")
        print(f"\n{name} ({sid})")
        aq_path = OUT / f"aq_{sid}.csv.gz"
        if aq_path.exists():
            print("    already downloaded, skipping")
            aq = read_saved(aq_path)
        else:
            aq, failed = fetch_station(sid, key)
            if failed:
                problems.append(f"{name}: pollutant years {failed} failed")
            elif not aq.empty:
                aq.to_csv(aq_path)
        rows.append(scout(sid, name, city, aq))

        if not isinstance(s["wx_file"], str):
            notes.append(f"{name}: no coordinates in the registry, so no weather file")
            continue
        wx_path = OUT / s["wx_file"]
        if not wx_path.exists():
            lat, lon = round(float(s["latitude"]), 1), round(float(s["longitude"]), 1)
            print(f"  weather for {lat}, {lon}...")
            wx, failed = fetch_weather(lat, lon)
            if failed:
                problems.append(f"{name}: weather for {', '.join(failed)} failed")
            elif not wx.empty:
                wx.to_csv(wx_path)

    cols = ["city", "station", "station_id", "good_winters", "winter_7to9am_median_pm25", "share_8am_poor", "first", "last"]
    report = pd.DataFrame(rows)
    report = report.reindex(columns=list(dict.fromkeys(cols + list(report.columns))))
    report = report.sort_values(["good_winters", "winter_7to9am_median_pm25"], ascending=False)
    report.to_csv(OUT / "scouting.csv", index=False)
    pd.set_option("display.width", 250, "display.max_columns", 30, "display.max_colwidth", 45)
    print("\nScouting (winter = Oct-Feb; good winter = PM2.5 present for 75%+ of hours; Poor = 91+ ug/m3):")
    print(report[cols].to_string(index=False))

    shutil.make_archive("saanssetu_data", "zip", OUT)
    size = Path("saanssetu_data.zip").stat().st_size / 1e6
    print(f"\nFinished in {(time.time() - t0) / 60:.1f} min. saanssetu_data.zip is {size:.1f} MB.")
    if size > 25:
        print("If the zip is too big to attach, send scouting.csv first and then only the files for the stations you pick.")
    for n in notes:
        print(f"Note: {n}")
    if problems:
        print("\nSome downloads failed. Run the same command again; finished files are kept:")
        for p in problems:
            print(f"  - {p}")


if __name__ == "__main__":
    main()
