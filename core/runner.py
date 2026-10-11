"""One tick of the system: for each town, pull the feed, run the pipeline, store the result, send at most one
alert, and score yesterday. Live runs and replays both come through here; only the source and the clock differ.

Each town is run on its own. A town that fails gets a 'failed' run document and the others carry on.
"""
import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

from . import check, evidence, ingest, pipeline
from .geo import minutes
from .store import ALERT, CHECK, CONFIG, EVIDENCE, LATEST, MORNING, REPLAY, RUN, RUN_TTL_DAYS, SUB, TOWN, MemoryStore
from .townconfig import alert_label, public_config


@lru_cache(maxsize=None)
def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_models(models_root, station_id, model_set="final"):
    """(config, model folder, metrics or None) for a station. model_set: 'final' to serve, 'models' for an honest replay."""
    sdir = Path(models_root) / station_id
    if not (sdir / "config.json").exists():
        raise FileNotFoundError(f"no trained models for {station_id} in {models_root}")
    if not (sdir / model_set / "calibration.json").exists():
        raise FileNotFoundError(f"{sdir / model_set} has no models")
    metrics = _json(str(sdir / "metrics.json")) if (sdir / "metrics.json").exists() else None
    return _json(str(sdir / "config.json")), sdir / model_set, metrics


def morning_call(morning):
    """The short version of the day's call, carried on every later run so the dashboard keeps showing it."""
    if not morning:
        return None
    a = morning["alert"]
    return {"run_id": morning["run_id"], "issued_at": morning["clock"]["now"], "school_flag": a["school_flag"],
            "alert_decision": a["decision"], "alert_reason": a["reason"], "message": a["message"]}


def town_line(town, run, cfg, metrics, alert_today):
    """One line for the town switcher."""
    label = alert_label(town, cfg) if cfg else None
    station = run.get("station") or {}
    if not cfg:
        chance = None
    elif label and metrics:
        chance = check.chance_verdict(metrics, label)
    else:
        chance = cfg.get("verdict_chance")
    return {"town_id": town["town_id"], "name": town["name"], "state": town.get("state"),
            "station": town["station"].get("name"), "has_routes": town["has_routes"],
            "now": station.get("latest"), "official_24h_band": (station.get("official_24h") or {}).get("band"),
            "alert_today": alert_today if town["has_routes"] else None,
            "school_flag": ((run.get("morning_call") or run["alert"])["school_flag"])["outdoor_pt"],
            "status": run["status"], "mode": run["mode"],
            "updated_at": run["clock"]["now"],
            "model": {"level": check.level_verdict(metrics, cfg) if cfg else None, "chance": chance, "chance_label": label}}


def run_town(store, town, models_root, source, now, mode="live", model_set="final", variant="now",
             notify=None, replay_of=None):
    """One town, one tick. Returns the run document it stored. Anything that goes wrong inside the run becomes a
    'failed' run document; only the store itself failing can raise, and run_all() reports that per town."""
    now = pd.Timestamp(now)
    town_id, day = town["town_id"], now.strftime("%Y-%m-%d")
    cfg = metrics = None
    earlier = None
    try:
        cfg, model_dir, metrics = load_models(models_root, town["station"]["station_id"], model_set)
        feed = ingest.sync(store, source, town, now)
        history = ingest.history(store, town, now)
        earlier = store.get(ALERT, town_id, day)
        subscribers = len(store.query(SUB, town_id)) if town["has_routes"] else 0
        run = pipeline.run_once(town, history, cfg, model_dir, now, mode=mode, variant=variant,
                                already_sent_today=earlier is not None, recipients=subscribers, replay_of=replay_of)
        run["feed"] = feed
        problems = [f"{kind}: {feed[kind]['error']}" for kind in ("aq", "wx") if feed[kind]["error"]]
        if problems:
            run["steps"][0]["detail"] += "; feed problem (" + "; ".join(problems) + ")"
        if run["alert"]["decision"] == "send":
            # Claim the day first: if two runs overlap, only the one that wins the claim sends.
            if store.put_if_absent(ALERT, town_id, day, run["alert"]):
                delivered, problem = 0, None
                if notify:
                    try:
                        delivered = notify(town, run)
                    except Exception as e:
                        problem = f"{type(e).__name__}: {str(e)[:200]}"
                run["alert"].update(recipients=delivered, delivery_problem=problem)
                store.put(ALERT, town_id, day, run["alert"])
            else:
                earlier = store.get(ALERT, town_id, day)
                run["alert"].update(decision="none", reason_code="already_sent_today", reason="An alert already went out this morning.",
                                    message=None, sent_at=None, channel=None, recipients=0)
                run["steps"][-1].update(status="not_needed", detail="already_sent_today")
        if earlier:
            run["alert"]["earlier_today"] = {k: earlier.get(k) for k in ("message", "sent_at", "recipients", "reason")}
    except Exception as e:
        run = pipeline.failed_run(town, now, mode, f"{type(e).__name__}: {str(e)[:300]}", replay_of=replay_of)

    # The day's call: the first run at or after the alert hour that has a forecast for school hours.
    covers_school = run["alert"]["school_flag"]["outdoor_pt"] != "unknown"
    if covers_school and now.hour * 60 + now.minute >= minutes(town["alert_rule"]["earliest"]):
        store.put_if_absent(MORNING, town_id, day, run)
    run["morning_call"] = morning_call(store.get(MORNING, town_id, day))

    stamp = now.strftime("%Y-%m-%dT%H:%M")
    store.put(RUN, town_id, stamp, run, ttl_days=RUN_TTL_DAYS)
    store.put(LATEST, town_id, "run", run)
    alert_today = store.get(ALERT, town_id, day) is not None
    store.put(TOWN, "all", town_id, town_line(town, run, cfg, metrics, alert_today))
    if cfg:
        store.put(CONFIG, town_id, "current", public_config(town, cfg, pipeline.SCHEMA_VERSION))
        try:
            # The wind-direction table is worked out once from the full archive (make_evidence.py) and saved beside
            # the models. The plant's direction and the sentences are added here, from the town file as it is now.
            stats_path = Path(models_root) / town["station"]["station_id"] / "evidence.json"
            if stats_path.exists():
                store.put(EVIDENCE, town_id, "current", evidence.describe(_json(str(stats_path)), town, pipeline.SCHEMA_VERSION))
        except Exception as e:   # the evidence panel must never cost a run
            run["evidence_problem"] = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            # Score yesterday once, on the first tick of today. A day the system was not running is not scored.
            yesterday = (now.normalize() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if store.get(CHECK, town_id, yesterday) is None and \
                    store.query(RUN, town_id, start=f"{yesterday}T00:00", end=f"{yesterday}T23:59", limit=1):
                check.update(store, town, cfg, metrics, yesterday, pipeline.SCHEMA_VERSION)
        except Exception as e:   # scoring yesterday must never cost today's run
            run["check_problem"] = f"{type(e).__name__}: {str(e)[:200]}"
    return run


def run_all(store, towns, models_root, source, now, **kwargs):
    """Every town, one after another. Returns {town_id: one-line result}."""
    out = {}
    for town_id, town in towns.items():
        try:
            run = run_town(store, town, models_root, source, now, **kwargs)
            out[town_id] = {"status": run["status"], "alert": run["alert"]["decision"], "reason": run["alert"]["reason_code"],
                            "steps": {s["step"]: s["status"] for s in run["steps"]}}
        except Exception as e:   # storing the result failed: report it and move to the next town
            out[town_id] = {"status": "failed", "alert": None, "reason": f"{type(e).__name__}: {str(e)[:200]}", "steps": {}}
    return out


def replay_day(towns, data_dir, models_root, day, start="05:00", end="09:00", step_minutes=15, lag_minutes=0,
               model_set="models", notify=None, store=None, park_at=None):
    """Runs a past day through the live path on a fake clock, tick by tick, then scores it as the nightly check would.
    Returns (store, {town_id: [run documents]}). The backtest models are the default, so the forecast is honest."""
    store = store or MemoryStore()
    source = ingest.ArchiveSource(data_dir, lag_minutes=lag_minutes)
    midnight = pd.Timestamp(day)
    frames, lines = {town_id: [] for town_id in towns}, {}
    for minute in range(minutes(start), minutes(end) + 1, step_minutes):
        now = midnight + pd.Timedelta(minutes=minute)
        for town_id, town in towns.items():
            run = run_town(store, town, models_root, source, now, mode="replay", model_set=model_set,
                           notify=notify, replay_of=str(midnight.date()))
            frames[town_id].append(run)
            lines[(town_id, now.strftime("%H:%M"))] = store.get(TOWN, "all", town_id)
            store.put(REPLAY, town_id, now.strftime("%Y-%m-%dT%H:%M"), run)
    late = midnight + pd.Timedelta(hours=23, minutes=45)
    for town_id, town in towns.items():
        try:
            cfg, _, metrics = load_models(models_root, town["station"]["station_id"], model_set)
            ingest.sync(store, source, town, late)   # the rest of the day's readings, as the feed would have delivered them
            check.update(store, town, cfg, metrics, str(midnight.date()), pipeline.SCHEMA_VERSION)
        except Exception:
            pass
    if park_at:   # leave "latest" showing this moment of the morning, e.g. 06:30 when the alert goes out
        for town_id in towns:
            parked = [r for r in frames[town_id] if r["clock"]["now"][11:16] == park_at]
            if parked:
                store.put(LATEST, town_id, "run", parked[0])
                store.put(TOWN, "all", town_id, lines[(town_id, park_at)])
    return store, frames
