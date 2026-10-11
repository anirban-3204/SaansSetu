"""
SaansSetu replay: run the whole pipeline for a past morning, exactly as it would have run live.

  python replay.py --town towns/asansol.json --at "2025-01-14 06:30"
  python replay.py --town towns/asansol.json --at "2025-01-14 06:30" --save run.json
  python replay.py --town towns/asansol.json --suggest            pick good mornings for the demo
  python replay.py --towns towns --day 2025-01-14                 the whole morning, every town, tick by tick

--at runs the pipeline once. --day runs the full system the way the Lambda does: readings arrive through
the store on a fake clock, every 15 minutes from 05:00 to 09:00, one alert at most, then the day is scored.
With --day you can add:
  --park 06:30         leave "latest" showing that moment
  --save-dir frames    write every API answer as a JSON file
  --table NAME         write into that DynamoDB table instead of memory (this is how the deployed demo is loaded)
  --send-to CHAT_ID    really send the Telegram alert to that chat (needs TELEGRAM_TOKEN)

It reads saanssetu_data/ (from fetch_data.py) and the newest out*/ folder (from train.py).
A reading stamped 06:00 covers 06:00 to 07:00, so it is used only from 07:00. What happened next is shown separately.

By default it uses the backtest models (trained only on data before the held-out winter), so a replay of
Oct 2024 to Feb 2025 is an honest forecast. --final uses the deployment models, which have seen that winter.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

import train
from core import pipeline
from core.bands import ORDER, band
from core.townconfig import load_town


def find_models(station_id, models=None):
    """The newest results folder that holds this station: out_v9 ... out_v2, out."""
    if models:
        roots = [Path(models)]
    else:
        roots = sorted(Path(".").glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [Path("out")]
    for root in roots:
        if (root / station_id / "config.json").exists():
            return root / station_id
    sys.exit(f"No trained models found for {station_id}. Run train.py --station {station_id} --final first.")


def setup(args):
    town = load_town(args.town)
    sid = town["station"]["station_id"]
    station_dir = find_models(sid, args.models)
    cfg = json.loads((station_dir / "config.json").read_text())
    model_dir = station_dir / ("final" if args.final else "models")
    if not (model_dir / "calibration.json").exists():
        sys.exit(f"{model_dir} has no models. Re-run train.py{' with --final' if args.final else ''}.")
    history = train.load(args.data, sid)
    return town, history, cfg, model_dir


def truth(history, run, hours=4):
    """What the station actually recorded after the reading the forecast was based on."""
    if not run.get("forecast"):
        return []
    out = []
    for h in run["forecast"]["hours"]:
        t = pd.Timestamp(h["time"][:16])
        v = history["pm25"].get(t)
        out.append({"time": h["time"], "actual": None if v is None or v != v else round(float(v), 1)})
    return out


def show(run):
    print(f"\n{run['town_id']}  {run['clock']['now']}  mode={run['mode']}  status={run['status']}")
    if run["status_message"]:
        print("  " + run["status_message"]["en"])
    for s in run["steps"]:
        print(f"  {s['step']:10} {s['status']:11} {s['detail'] or ''}")
    st = run.get("station")
    if st:
        print(f"\n  Latest finished hour, starting {st['latest']['time'][11:16]}: {st['latest']['pm25']} ({st['latest']['band']})"
              f"   |   city badge (24 h average): {st['official_24h']['pm25_avg']} ({st['official_24h']['band']})")
    fc = run.get("forecast")
    if fc:
        high = fc["bad_case"]
        real = {x["time"]: x["actual"] for x in run.get("replay_truth", [])}
        print(f"\n  Forecast ({fc['feature_set']} models)   likely   bad case   chance                       actually     (each row is the hour starting then)")
        for h in fc["hours"]:
            ch = "  ".join(f"{k} {v:.0%}" for k, v in h["chance"].items())
            actual = real.get(h["time"], "")
            print(f"    {h['time'][11:16]}              {h['p50']:>6}   {h[high]:>8}   {ch:28} {'no reading' if actual is None else actual}")
    for r in run["routes"]:
        p = r["summary"]["peak"]
        print(f"\n  {r['name']}: peaks {p['pm25']} ({p['band']}) at {p['time']}")
    d = run["departures"]
    if d:
        u, b = d["usual"], d["best"]
        print(f"\n  Usual: {u['route_id']} leave {u['leave']}  dose {u['dose']}")
        if b:
            print(f"  Best:  {b['route_id']} leave {b['leave']}  dose {b['dose']}   -> {d['saving_vs_usual_pct']}% less "
                  f"({d['saving_from_route_change_only_pct']}% from the route change alone)")
    a = run["alert"]
    print(f"\n  Alert: {a['decision'].upper()} ({a['reason_code']}) {a['reason']}")
    if a["message"]:
        print(f"    EN: {a['message']['en']}\n    HI: {a['message']['hi']}")
    print(f"  School: PT {a['school_flag']['outdoor_pt']}. {a['school_flag']['reason']}")


def suggest(town, history, cfg, model_dir, args):
    """Mornings where the street was worse than the city badge suggested, the alert fired and the forecast was close."""
    pm = history["pm25"]
    days = pd.date_range(args.date_from, args.date_to, freq="D")
    rows = []
    for d in days:
        # At 06:30 the newest finished hour is the one stamped 05:00 (05:00 to 06:00). That is all the badge can show.
        five, eight = d + pd.Timedelta(hours=5), d + pd.Timedelta(hours=8)
        if pd.isna(pm.get(five)) or pd.isna(pm.get(eight)):
            continue
        avg24 = pm.reindex(pd.date_range(five - pd.Timedelta(hours=23), five, freq="h")).mean()
        rows.append((d, float(avg24), float(pm[eight])))
    rows.sort(key=lambda r: r[2] - r[1], reverse=True)
    out = []
    for d, avg24, actual8 in rows[: args.top * 3]:
        run = pipeline.run_once(town, history, cfg, model_dir, d + pd.Timedelta(hours=6, minutes=30), mode="replay", variant=args.variant)
        if not run["departures"]:
            continue
        f8 = next((h["p50"] for h in run["forecast"]["hours"] if h["time"][11:16] == "08:00"), None)
        usual = next(r for r in run["routes"] if r["route_id"] == run["departures"]["usual"]["route_id"])
        out.append({"date": str(d.date()), "badge": band(avg24), "badge_pm25": round(avg24), "actual_8am": round(actual8),
                    "forecast_8am": f8, "miss": None if f8 is None else round(abs(f8 - actual8)),
                    "street_peak": usual["summary"]["peak"]["band"], "alert": run["alert"]["decision"],
                    "saving_pct": run["departures"]["saving_vs_usual_pct"]})
    good = [r for r in out if r["alert"] == "send" and ORDER.index(r["street_peak"]) > ORDER.index(r["badge"])]
    good.sort(key=lambda r: r["miss"] if r["miss"] is not None else 1e9)
    table = pd.DataFrame(good[: args.top] or out[: args.top])
    pd.set_option("display.width", 200)
    print(f"\nMornings between {args.date_from} and {args.date_to} worth replaying"
          f"{'' if good else ' (none met every condition; showing the closest)'}:")
    print("badge = what the city 24-hour average showed at 6:30 AM. street_peak = worst band on the usual route. "
          "miss = forecast error for the hour starting 8 AM.\n")
    print(table.to_string(index=False) if len(table) else "No morning in that range has readings at 6 AM and 8 AM.")
    if len(table):
        print(f'\nReplay one:  python replay.py --town {args.town} --at "{table.iloc[0]["date"]} 06:30"')


def replay_day(args):
    """The whole morning through the live path: store, runner, one alert at most, nightly check."""
    from core import api, runner, telegram
    from core.store import SUB, DynamoStore, MemoryStore, create_table
    from core.townconfig import load_towns
    if args.towns:
        towns, problems = load_towns(args.towns)
        for name, why in problems.items():
            print(f"Skipping {name}: {why}")
    else:
        town = load_town(args.town)
        towns = {town["town_id"]: town}
    if not towns:
        sys.exit("No usable town files.")
    root = find_models(next(iter(towns.values()))["station"]["station_id"], args.models).parent
    if args.table:
        create_table(args.table)
        store = DynamoStore(args.table)
    else:
        store = MemoryStore()
    notify = None
    if args.send_to:
        if not os.environ.get("TELEGRAM_TOKEN"):
            sys.exit("--send-to needs the bot token in the TELEGRAM_TOKEN environment variable.")
        bot = telegram.Bot(os.environ["TELEGRAM_TOKEN"])
        for town_id, town in towns.items():
            if town["has_routes"]:
                store.put(SUB, town_id, args.send_to, {"chat_id": args.send_to, "joined_at": None})
        notify = lambda town, run: telegram.notify(store, bot, town, run)

    print(f"Replaying {args.day} for {', '.join(towns)}; models from {root}{os.sep}{'final' if args.final else 'models'}")
    if args.final:
        print("Caution: the deployment models may have been trained on this day. Drop --final for an honest replay.")
    store, frames = runner.replay_day(towns, args.data, root, args.day, lag_minutes=args.lag, notify=notify, store=store,
                                      model_set="final" if args.final else "models", park_at=args.park)
    for town_id, runs in frames.items():
        print(f"\n{town_id}")
        print("  time   status    reading  alert                           PT flag      (reading = start of the newest finished hour)")
        for r in runs:
            a = r["alert"]
            reading = (r.get("station") or {}).get("latest", {}).get("time", "")[11:16] or "-"
            print(f"  {r['clock']['now'][11:16]}  {r['status']:9} {reading:7}  {(a['decision'] + ' (' + a['reason_code'] + ')'):31} {a['school_flag']['outdoor_pt']}")
            if r["status"] == "failed":
                print(f"         {r['steps'][-1]['detail']}")
            if a["decision"] == "send":
                print(f"         EN: {a['message']['en']}\n         HI: {a['message']['hi']}\n         delivered to {a['recipients']}")
        rec = (store.get("accuracy", town_id, "current") or {}).get("yesterday")
        if rec and rec["hours"]:
            line = ", ".join(f"{h['time']} said {h['forecast_p50']} got {h['actual']}" for h in rec["hours"])
            print(f"  Scored: {line}. Outcome: {rec['outcome']} (flagged {rec['flagged']}, {rec['label'] or 'poor'} air happened: {rec['was_bad']})")
    if args.save_dir:
        out = Path(args.save_dir)
        paths = ["/towns"] + [f"/towns/{t}/{p}" for t in towns for p in ("config", "run/latest", "run/morning", "accuracy", "evidence", f"replay/{args.day}")]
        for path in paths:
            status, body = api.handle(store, "GET", path)
            if status == 200:
                file = out / (path.strip("/").replace("/", "_") + ".json")
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved the API answers to {out}{os.sep}")
    if args.table:
        print(f"\nWritten to DynamoDB table {args.table}." + (f' "latest" shows {args.park}.' if args.park else ""))


def main():
    if hasattr(sys.stdout, "reconfigure"):   # Hindi text must print even when output is piped to a file on Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Replay SaansSetu for a past morning.")
    ap.add_argument("--town", help="town file, e.g. towns/asansol.json")
    ap.add_argument("--towns", help="folder of town files: with --day, replay every town in it")
    ap.add_argument("--day", help="replay this whole morning through the live path, e.g. 2025-01-14")
    ap.add_argument("--park", default=None, help='with --day: the time "latest" is left showing, e.g. 06:30')
    ap.add_argument("--save-dir", help="with --day: write every API answer as JSON into this folder")
    ap.add_argument("--table", help="with --day: write into this DynamoDB table")
    ap.add_argument("--lag", type=int, default=0, help="with --day: minutes the pollutant feed runs behind")
    ap.add_argument("--send-to", help="with --day: send the Telegram alert to this chat id")
    ap.add_argument("--at", help='clock time of the run, e.g. "2025-01-14 06:30"')
    ap.add_argument("--data", default="saanssetu_data")
    ap.add_argument("--models", default=None, help="results folder from train.py (default: newest out*/)")
    ap.add_argument("--variant", choices=["now", "fut"], default="now", help="now = current weather only (default); fut = with weather forecast")
    ap.add_argument("--final", action="store_true", help="use the deployment models instead of the backtest models")
    ap.add_argument("--save", help="write the run document to this file")
    ap.add_argument("--suggest", action="store_true", help="list good mornings to replay")
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--to", dest="date_to", default=None)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    if args.day:
        if not (args.town or args.towns):
            sys.exit("Give --town or --towns with --day.")
        return replay_day(args)
    if not args.town:
        sys.exit("Give a town file with --town.")

    town, history, cfg, model_dir = setup(args)
    held_out = (pd.Timestamp(cfg["test"][0]), pd.Timestamp(cfg["test"][1]) + pd.Timedelta(hours=23))
    print(f"Town {town['name']}, station {town['station']['station_id']}, models from {model_dir}")
    if town.get("geometry_note"):
        print(f"Note: {town['geometry_note']}")

    if args.suggest:
        args.date_from = args.date_from or cfg["test"][0]
        args.date_to = args.date_to or cfg["test"][1]
        return suggest(town, history, cfg, model_dir, args)
    if not args.at:
        sys.exit('Give a time with --at "YYYY-MM-DD HH:MM", or use --suggest.')

    now = pd.Timestamp(args.at)
    if args.final and now <= held_out[1]:
        print("Caution: the deployment models were trained on this period, so this replay flatters the forecast. Drop --final for an honest one.")
    elif not args.final and now < held_out[0]:
        print(f"Caution: {now.date()} is inside the training data, so this forecast looks better than it would live. "
              f"Honest replays are from {cfg['test'][0]} to {cfg['test'][1]}.")
    run = pipeline.run_once(town, history, cfg, model_dir, now, mode="replay", variant=args.variant, replay_of=str(now.date()))
    run["replay_truth"] = truth(history, run)
    show(run)
    if args.save:
        Path(args.save).write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved {args.save}")


if __name__ == "__main__":
    main()
