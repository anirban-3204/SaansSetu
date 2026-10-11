"""
SaansSetu: write a town file for every station whose model passed, so the system carries all of them.

  python make_towns.py                      newest out*/ folder, saanssetu_data/, towns/
  python make_towns.py --models out_v3

A station that already has a town file is left alone (so a hand-made file with routes, like
towns/asansol.json, is never touched). New files are forecast-only: station, forecast and school flag.
Add routes to one later with make_routes.py.

Which chance label drives the school flag: "poor" (91+) when that model passed its own test on the
held-out winter, so the flag means the same thing in every town. Otherwise the label train.py suggested.

"Passed" is judged on the alert a family can act on: issued at 6 AM from the reading for 5 to 6 AM
(train.py calls this case lagged_5am). The table shows that case's skill for every station.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

from core import check
from core.townconfig import load_towns, prepare


def newest_models():
    roots = sorted(Path(".").glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [Path("out")]
    return next((r for r in roots if r.is_dir()), None)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def city_of(row, station_name):
    """City from the registry, else from a name like 'Raghunathpali, Rourkela - OSPCB'."""
    city = row.get("city_name")
    if isinstance(city, str) and city.strip():
        return city.strip()
    return station_name.rsplit(" - ", 1)[0].split(",")[-1].strip()


def choose_label(cfg, metrics):
    if "poor" in cfg.get("labels", []) and check.chance_verdict(metrics, "poor") == "USE":
        return "poor", "poor passed its test"
    suggested = cfg.get("suggested_label")
    return suggested, f"poor did not pass or was not trained; using train.py's suggestion ({cfg.get('verdict_chance')})"


def build(models, data, towns_dir):
    """Returns (files to write as {path: town}, report rows)."""
    stations = pd.read_csv(Path(data) / "stations.csv").set_index("station_id")
    existing, problems = load_towns(towns_dir) if Path(towns_dir).is_dir() else ({}, {})
    taken = {t["station"]["station_id"]: tid for tid, t in existing.items()}
    used_ids, files, rows = set(existing), {}, []
    for cfg_path in sorted(Path(models).glob("*/config.json")):
        cfg = json.loads(cfg_path.read_text())
        sid, name = cfg["station_id"], str(cfg.get("station", cfg["station_id"])).rsplit(" - ", 1)[0]
        metrics_path = cfg_path.parent / "metrics.json"
        metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else None
        level = check.level_verdict(metrics, cfg)
        scored = (metrics or {}).get("now", {}).get(check.CASE) or {}
        row = {"station_id": sid, "station": name, "level": level, "6am_alert_skill": scored.get("skill_vs_persistence"),
               "ci95": scored.get("skill_vs_persistence_ci95")}
        if sid in taken:
            rows.append({**row, "town": taken[sid], "file": "kept (already exists)"})
            continue
        if level != "SHIP":
            rows.append({**row, "town": "-", "file": "skipped: the model did not pass"})
            continue
        if sid not in stations.index or pd.isna(stations.loc[sid].get("latitude")) or pd.isna(stations.loc[sid].get("longitude")):
            rows.append({**row, "town": "-", "file": "skipped: no coordinates in stations.csv"})
            continue
        st = stations.loc[sid]
        city = city_of(st, str(cfg.get("station", sid)))
        town_id = slug(city)
        if town_id in used_ids:
            town_id = f"{town_id}-{slug(sid)}"
        used_ids.add(town_id)
        label, why = choose_label(cfg, metrics)
        state = next((str(st[c]) for c in ("state_name", "state") if c in st and isinstance(st[c], str)), None)
        town = {"town_id": town_id, "name": city, "state": state,
                "station": {"station_id": sid, "name": name, "lat": round(float(st["latitude"]), 6), "lon": round(float(st["longitude"]), 6)}}
        if label:
            town["alert_label"] = label
        prepare(json.loads(json.dumps(town)))   # must load cleanly before it is written
        files[Path(towns_dir) / f"{town_id}.json"] = town
        rows.append({**row, "town": town_id, "file": f"{town_id}.json", "school_flag_label": label, "why": why})
    return files, rows, problems


def main():
    ap = argparse.ArgumentParser(description="Write a town file for every station whose model passed.")
    ap.add_argument("--models", default=None, help="results folder from train.py (default: newest out*/)")
    ap.add_argument("--data", default="saanssetu_data")
    ap.add_argument("--towns", default="towns")
    args = ap.parse_args()
    models = Path(args.models) if args.models else newest_models()
    if models is None or not list(models.glob("*/config.json")):
        sys.exit("No trained stations found. Run train.py first, or pass --models.")
    if not (Path(args.data) / "stations.csv").exists():
        sys.exit(f"{args.data}/stations.csv not found. It is written by fetch_data.py.")
    files, rows, problems = build(models, args.data, args.towns)
    Path(args.towns).mkdir(exist_ok=True)
    for path, town in files.items():
        path.write_text(json.dumps(town, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pd.set_option("display.width", 220, "display.max_colwidth", 60)
    print(f"Stations in {models}/:\n")
    print(pd.DataFrame(rows).fillna("").to_string(index=False))
    for name, why in problems.items():
        print(f"\nProblem in an existing town file, {name}: {why}")
    print(f"\nWrote {len(files)} new town file(s) to {args.towns}/. Check 'name' and 'state' in each; they come from the station registry.")


if __name__ == "__main__":
    main()
