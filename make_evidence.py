"""
SaansSetu: work out, for every town, which wind direction the dirtiest winter mornings arrive from.

  python make_evidence.py                     every town in towns/, newest out*/ folder, saanssetu_data/
  python make_evidence.py --models out_v3

It counts hours in the station archive (the conditional probability method, CBPF) and writes
<models>/<station_id>/evidence.json. The backend publishes it at /towns/<id>/evidence on its next run,
and pack_models.py carries it into the deploy bundle. Run it again only when the archive changes.

It says "no clear direction" when that is what the data shows. One station plus area wind shows a
direction, not proof of a source.
"""
import argparse
import json
import sys
from pathlib import Path

import train
from core import SCHEMA_VERSION, evidence
from core.townconfig import load_towns


def newest_models():
    roots = sorted(Path(".").glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [Path("out")]
    return next((r for r in roots if r.is_dir()), None)


def build_all(towns, data, models):
    """Writes evidence.json for each town's station. Returns {town_id: the document the dashboard will get}."""
    out = {}
    for town_id, town in towns.items():
        sid = town["station"]["station_id"]
        folder = Path(models) / sid
        if not folder.is_dir():
            out[town_id] = None
            continue
        stats = evidence._plain(evidence.station_stats(train.load(data, sid), sid))
        (folder / "evidence.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False), encoding="utf-8")
        out[town_id] = evidence.describe(stats, town, SCHEMA_VERSION)
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Wind-direction evidence for every town.")
    ap.add_argument("--models", default=None, help="results folder from train.py (default: newest out*/)")
    ap.add_argument("--data", default="saanssetu_data")
    ap.add_argument("--towns", default="towns")
    args = ap.parse_args()
    models = Path(args.models) if args.models else newest_models()
    if models is None:
        sys.exit("No out*/ folder found. Run train.py first, or pass --models.")
    towns, problems = load_towns(args.towns)
    for name, why in problems.items():
        print(f"Skipping {name}: {why}")
    if not towns:
        sys.exit(f"No usable town files in {args.towns}/")
    for town_id, doc in build_all(towns, args.data, models).items():
        if doc is None:
            print(f"\n{town_id}: no models folder for its station in {models}/, skipped")
            continue
        p = doc.get("period", {})
        print(f"\n{town_id}: {doc['verdict'].upper().replace('_', ' ')}"
              f"   ({p.get('hours_used')} winter-morning hours on {p.get('days_used')} days, {p.get('from')} to {p.get('to')})")
        if doc.get("why"):
            print(f"  {doc['why']}")
        if doc.get("threshold"):
            t = doc["threshold"]
            print(f"  dirtiest quarter starts at about {t['pm25_typical']} ug/m3 ({t['band_typical']}); it is set per month and hour, from {t['pm25_lowest']} to {t['pm25_highest']}")
        s = doc.get("top_sector")
        if s:
            print(f"  top sector: wind from {s['wind_from'][0]} to {s['wind_from'][1]}: {round(s['share_of_high_hours'] * 100)}% of the dirtiest hours, "
                  f"{round(s['share_of_all_hours'] * 100)}% of all hours. {s['lift']} times as often, 95% range {s['lift_ci95']}, same sector in {round(s['stable_in'] * 100)}% of resamples")
        if doc.get("plant"):
            pl = doc["plant"]
            print(f"  plant lies to the {pl['label']} of the station ({pl['bearing_from_station_deg']} deg)"
                  + (", but its location is a PLACEHOLDER, so nothing is said about it" if pl["location_is_placeholder"] else f"; inside the top sector: {pl['inside_top_sector']}"))
        print(f"  EN: {doc['summary']['en']}")
        print(f"  HI: {doc['summary']['hi']}")
    print(f"\nSaved evidence.json beside each station's models in {models}/")


if __name__ == "__main__":
    main()
