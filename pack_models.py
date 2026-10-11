"""
SaansSetu: collect what the Lambda needs into one folder, and nothing else.

  python pack_models.py                                      every town in towns/, newest out*/ folder
  python pack_models.py --days 2024-12-01 2024-12-20         also cut the archive down to those days, for replay

Writes deploy/:
  models/<station_id>/   config.json, metrics.json, and the 'now' models from final/ (serving) and models/ (honest replay)
  saanssetu_data/        with --days: each town's readings and weather for those days plus the 17 days before
  towns/                 the town files

The 'fut' models (the ones that need a weather forecast) are left out: serving uses 'now'.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

import train
from core.townconfig import load_towns

LOOKBACK_DAYS = 17


def newest_models():
    roots = sorted(Path(".").glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [Path("out")]
    return next((r for r in roots if r.is_dir()), None)


def size_mb(folder):
    return sum(f.stat().st_size for f in Path(folder).rglob("*") if f.is_file()) / 1e6


def pack_station(models, sid, out):
    src, dst = Path(models) / sid, Path(out) / "models" / sid
    if not (src / "config.json").exists():
        return f"no models in {src}"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "metrics.json", "evidence.json"):
        if (src / name).exists():
            shutil.copy(src / name, dst / name)
    missing = []
    for model_set in ("final", "models"):
        if not (src / model_set / "calibration.json").exists():
            missing.append(model_set)
            continue
        (dst / model_set / "now").mkdir(parents=True, exist_ok=True)
        shutil.copy(src / model_set / "calibration.json", dst / model_set / "calibration.json")
        for f in (src / model_set / "now").glob("*.txt"):
            shutil.copy(f, dst / model_set / "now" / f.name)
    return ("missing " + " and ".join(missing) + " (train.py --final makes final/)") if missing else None


def pack_data(data, sid, out, first, last):
    data, dst = Path(data), Path(out) / "saanssetu_data"
    dst.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(first) - pd.Timedelta(days=LOOKBACK_DAYS), pd.Timestamp(last) + pd.Timedelta(days=1, hours=23)
    aq = train._read(data / f"aq_{sid}.csv.gz").loc[start:end]
    aq.to_csv(dst / f"aq_{sid}.csv.gz")
    wx_path = train.find_weather(data, sid)
    if wx_path is not None:
        train._read(wx_path).loc[start:end].to_csv(dst / f"wx_{sid}.csv.gz")   # found by station id, so stations.csv is not needed
    days = pd.date_range(first, last, freq="D")
    # the 6 AM alert rests on the reading for 05:00 to 06:00, which is stamped 05:00
    usable = aq["pm25"].reindex(days + pd.Timedelta(hours=5)).notna().sum() if "pm25" in aq else 0
    return len(aq), int(usable), len(days)


def main():
    ap = argparse.ArgumentParser(description="Collect the models, towns and replay data the Lambda needs.")
    ap.add_argument("--models", default=None, help="results folder from train.py (default: newest out*/)")
    ap.add_argument("--towns", default="towns")
    ap.add_argument("--data", default="saanssetu_data")
    ap.add_argument("--out", default="deploy")
    ap.add_argument("--days", nargs=2, metavar=("FIRST", "LAST"), help="also pack the archive for these days, e.g. 2024-12-01 2024-12-20")
    args = ap.parse_args()
    models = Path(args.models) if args.models else newest_models()
    if models is None:
        sys.exit("No out*/ folder found. Run train.py first, or pass --models.")
    towns, problems = load_towns(args.towns)
    for name, why in problems.items():
        print(f"Skipping {name}: {why}")
    if not towns:
        sys.exit(f"No usable town files in {args.towns}/")
    out = Path(args.out)
    (out / "towns").mkdir(parents=True, exist_ok=True)
    print(f"Packing {len(towns)} town(s) from {models}/ into {out}/\n")
    files = {json.loads(p.read_text(encoding="utf-8")).get("town_id"): p for p in Path(args.towns).glob("*.json")
             if p.name not in problems}
    for town_id, town in towns.items():
        sid = town["station"]["station_id"]
        shutil.copy(files[town_id], out / "towns" / f"{town_id}.json")
        problem = pack_station(models, sid, out)
        line = f"  {town_id:15} {sid:10} models {size_mb(out / 'models' / sid):6.1f} MB" if (out / "models" / sid).exists() else f"  {town_id:15} {sid:10}"
        if args.days and (Path(args.data) / f"aq_{sid}.csv.gz").exists():
            rows, usable, days = pack_data(args.data, sid, out, *args.days)
            line += f"   data {rows} hours, 5-to-6 AM reading on {usable} of {days} days"
        print(line + (f"   PROBLEM: {problem}" if problem else ""))
    print(f"\nTotal: {size_mb(out):.1f} MB in {out}/")
    print("In the Lambda set SAANSSETU_MODELS, SAANSSETU_TOWNS and SAANSSETU_DATA to these three folders.")


if __name__ == "__main__":
    main()
