"""
SaansSetu: check that fetch_data.py downloaded everything and that it is usable for training.

  python check_data.py                 (looks in ./saanssetu_data)
  python check_data.py some_folder

It checks, for every station: the pollutant file, its date range and gaps, the weather file,
and whether there is enough history to train and test. It ends with the stations to use.
"""

import sys
import zipfile
from pathlib import Path

import pandas as pd

START, END = "2019-10-01", "2025-09-30"
TEST = ("2024-10-01", "2025-02-28 23:00")       # held-out winter in train.py
VAL = ("2023-10-01", "2024-02-29 23:00")        # winter before it (early stopping, calibration)
WX_NEEDED = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m"]
WX_NICE = ["boundary_layer_height", "dew_point_2m", "precipitation", "surface_pressure", "cloud_cover"]

problems, warnings = [], []


def read(path):
    return pd.read_csv(path, parse_dates=["time"], index_col="time")


def check_pollutants(path, name):
    """Returns a dict of facts, or None if the file is unusable."""
    try:
        aq = read(path)
    except Exception as e:
        problems.append(f"{name}: cannot read {path.name} ({e.__class__.__name__})")
        return None
    if "pm25" not in aq or aq["pm25"].notna().sum() == 0:
        problems.append(f"{name}: no PM2.5 values")
        return None
    if not aq.index.is_unique:
        problems.append(f"{name}: {aq.index.duplicated().sum()} duplicate timestamps")
    if not aq.index.is_monotonic_increasing:
        problems.append(f"{name}: timestamps are out of order")
    off_hour = (aq.index.minute != 0).mean()
    if off_hour > 0.01:
        warnings.append(f"{name}: {off_hour:.0%} of readings are not on the hour")
    pm = aq["pm25"].dropna()
    pm = pm[~pm.index.duplicated()]
    bad = ((pm <= 0) | (pm >= 1500)).mean()
    if bad > 0.02:
        warnings.append(f"{name}: {bad:.1%} of PM2.5 values are zero, negative or above 1500 (train.py drops them)")
    span = pd.date_range(pm.index.min().floor("h"), pm.index.max().floor("h"), freq="h")
    hourly = pm.groupby(pm.index.floor("h")).mean().reindex(span)
    gap = hourly.isna().astype(int).groupby(hourly.notna().cumsum()).sum().max()

    def share(a, b):
        idx = pd.date_range(a, b, freq="h")
        return float(hourly.reindex(idx).notna().mean())

    before_test = hourly[hourly.index < TEST[0]]
    train_hours = int(before_test[(before_test.index < VAL[0]) | (before_test.index > VAL[1])].notna().sum())
    test_hours = int(hourly.reindex(pd.date_range(*TEST, freq="h")).notna().sum())
    return {
        "first": str(pm.index.min().date()), "last": str(pm.index.max().date()),
        "present": float(hourly.notna().mean()), "longest_gap_days": round(int(gap) / 24, 1),
        "train_hours": train_hours, "val_winter": share(*VAL), "test_winter": share(*TEST), "test_hours": test_hours,
        "median_pm25": float(pm.median()),
        "other": ", ".join(c for c in ["pm10", "no2", "co", "so2"] if c in aq and aq[c].notna().mean() > 0.3) or "none",
    }


def check_weather(path, name):
    try:
        wx = read(path)
    except Exception as e:
        problems.append(f"{name}: cannot read {path.name} ({e.__class__.__name__})")
        return "unreadable"
    full = pd.date_range(START, f"{END} 23:00", freq="h")
    missing = len(full.difference(wx.index))
    if missing:
        problems.append(f"{name}: weather is missing {missing:,} of {len(full):,} hours ({path.name})")
    lacking = [c for c in WX_NEEDED if c not in wx or wx[c].isna().mean() > 0.02]
    if lacking:
        problems.append(f"{name}: weather lacks {', '.join(lacking)}")
    weak = [c for c in WX_NICE if c not in wx or wx[c].isna().mean() > 0.2]
    if weak:
        warnings.append(f"{name}: weather has little or no {', '.join(weak)} (training still works, with fewer inputs)")
    if "wind_direction_10m" in wx and not wx["wind_direction_10m"].dropna().between(0, 360).all():
        problems.append(f"{name}: wind direction outside 0-360")
    if "wind_speed_10m" in wx and wx["wind_speed_10m"].max() > 40:
        warnings.append(f"{name}: wind speed up to {wx['wind_speed_10m'].max():.0f}; expected metres per second")
    return "ok" if not missing and not lacking else "incomplete"


def main():
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "saanssetu_data")
    if not (d / "stations.csv").exists():
        sys.exit(f"{d / 'stations.csv'} not found. Run this from the folder where you ran fetch_data.py.")
    stations = pd.read_csv(d / "stations.csv")
    print(f"Checking {len(stations)} stations in {d}/ ...\n")

    rows, wx_seen = [], {}
    for _, s in stations.iterrows():
        sid = s["station_id"]
        name = str(s.get("station_name", sid))
        row = {"city": s.get("city_name", ""), "station": name[:38], "station_id": sid}
        aq_path = d / f"aq_{sid}.csv.gz"
        facts = check_pollutants(aq_path, name) if aq_path.exists() else None
        if not aq_path.exists():
            problems.append(f"{name}: pollutant file {aq_path.name} is missing")

        wx_file = s.get("wx_file")
        if not isinstance(wx_file, str):
            wx = "none"
            warnings.append(f"{name}: no coordinates in the registry, so no weather")
        elif not (d / wx_file).exists():
            wx = "missing"
            problems.append(f"{name}: weather file {wx_file} is missing")
        else:
            if wx_file not in wx_seen:
                wx_seen[wx_file] = check_weather(d / wx_file, name)
            wx = wx_seen[wx_file]
        row["weather"] = wx

        if facts:
            row.update(first=facts["first"], last=facts["last"], pm25_present=f"{facts['present']:.0%}",
                       gap_days=facts["longest_gap_days"], train_hours=facts["train_hours"],
                       val_winter=f"{facts['val_winter']:.0%}", test_winter=f"{facts['test_winter']:.0%}",
                       median=round(facts["median_pm25"]), other=facts["other"])
            why = []
            if facts["train_hours"] < 3000:
                why.append(f"only {facts['train_hours']:,} training hours (needs 3,000)")
            if facts["test_hours"] < 200:
                why.append("test winter nearly empty")
            if wx in ("missing", "incomplete", "unreadable"):
                why.append(f"weather {wx}")
            ok = "YES" if wx == "ok" else "YES, without weather"
            row["ready"] = ok if not why else "NO: " + "; ".join(why)
            row["_score"] = (not why, wx == "ok", facts["test_winter"], facts["train_hours"])
            if not why and facts["test_winter"] < 0.6:
                warnings.append(f"{name}: test winter is only {facts['test_winter']:.0%} complete, so the scores will be noisy")
            if not why and facts["val_winter"] < 0.14:
                warnings.append(f"{name}: winter 2023-24 is nearly empty; train.py will fall back to another validation period")
        else:
            row["ready"] = "NO: no usable pollutant file"
            row["_score"] = (False, False, 0, 0)
        rows.append(row)

    table = pd.DataFrame(rows)
    for c in ["train_hours", "median"]:
        if c in table:
            table[c] = table[c].astype("Int64")
    cols = ["city", "station", "station_id", "first", "last", "pm25_present", "gap_days", "train_hours",
            "val_winter", "test_winter", "median", "weather", "ready"]
    pd.set_option("display.width", 260, "display.max_columns", 30, "display.max_colwidth", 60)
    print(table.reindex(columns=cols).to_string(index=False))
    print("\ntrain_hours = PM2.5 hours available for training. val_winter / test_winter = share of hours with PM2.5")
    print("in Oct 2023-Feb 2024 and Oct 2024-Feb 2025. gap_days = longest stretch with no PM2.5.")

    # leftover files and the zip
    expected = {"stations.csv", "scouting.csv"} | {f"aq_{s}.csv.gz" for s in stations["station_id"]} | \
               {w for w in stations.get("wx_file", pd.Series(dtype=object)) if isinstance(w, str)}
    present = {p.name for p in d.iterdir()}
    if "scouting.csv" not in present:
        warnings.append("scouting.csv is missing: the download did not reach the end")
    z = Path("saanssetu_data.zip")
    if z.exists():
        try:
            inside = set(zipfile.ZipFile(z).namelist())
            if expected & present - inside:
                warnings.append(f"saanssetu_data.zip is older than the folder ({len(expected & present - inside)} files not in it); re-run fetch_data.py to rebuild it")
        except zipfile.BadZipFile:
            problems.append("saanssetu_data.zip is corrupt; re-run fetch_data.py to rebuild it")
    cache = Path("saanssetu_cache")
    if cache.exists():
        print(f"\nWeather cache: {len(list(cache.glob('wx_*.csv')))} quarter files in {cache}/ (safe to delete once everything below is fine)")

    print("\nPROBLEMS" if problems else "\nNo problems found.")
    for p in problems:
        print(f"  - {p}")
    if warnings:
        print("\nWorth knowing")
        for w in warnings:
            print(f"  - {w}")

    ready = table[table["ready"].str.startswith("YES")]
    if len(ready):
        best = ready.sort_values("_score", ascending=False).groupby("city", sort=False).head(1)
        print("\nBest station per town (weather present, then most complete test winter, then most history):")
        for _, r in best.iterrows():
            print(f"  {r['city']:14} {r['station_id']:11} {r['station']}")
        ids = " ".join(best["station_id"])
        print(f"\nTo train on all of them:\n  python train.py --station {ids} --final")
    else:
        print("\nNo station is ready to train yet.")
    if problems:
        print("\nFix: run the same fetch_data.py command again. It keeps finished files and only fetches what is missing.")
        print("If a file is listed as unreadable, out of order or with duplicates, delete that one file first.")
        sys.exit(1)


if __name__ == "__main__":
    main()
