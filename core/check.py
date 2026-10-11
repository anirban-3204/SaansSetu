"""The nightly check: the morning's forecast against what the station then recorded, and the running score.

The forecast that gets scored is fixed in advance: the first full run at or after the alert hour (the 'morning'
document). Nothing is picked after the fact.
"""
from datetime import date as Date, timedelta

from .store import ACCURACY, ALERT, AQ, CHECK, MORNING
from .townconfig import alert_label

# train.py scores two cases by the stamp of the newest reading used. Stamps mark the start of an hour, so:
#   lagged_5am  reading for 05:00 to 06:00, known at 06:00  -> the alert a family can act on. This is the one we report.
#   fresh_6am   reading for 06:00 to 07:00, known at 07:00  -> too late for a 7 AM walk
CASE = "lagged_5am"

OUTCOME = {(True, True): "hit", (False, True): "miss", (True, False): "false_alarm", (False, False): "correct_quiet"}


def _mean(values):
    return round(sum(values) / len(values), 1) if values else None


def level_verdict(metrics, model_cfg=None, variant="now", case=CASE):
    """train.py's ship rule applied to the case we actually serve: beats persistence, and by enough to rule out luck.
    Falls back to the verdict train.py stored when the metrics file is missing."""
    a = (metrics or {}).get(variant, {}).get(case) or {}
    low = (a.get("skill_vs_persistence_ci95") or [None])[0]
    if "skill_vs_persistence_ci95" not in a:
        return (model_cfg or {}).get("verdict_level") or "NO VERDICT"
    return "SHIP" if low is not None and low > 0 else "DO NOT SHIP"


def chance_verdict(metrics, label, variant="now", case=CASE):
    """Same rule train.py prints, for any label: must beat the usual rate, and by enough to rule out luck."""
    c = ((metrics or {}).get(variant, {}).get(case) or {}).get("chance", {}).get(label)
    if not c:
        return "NO VERDICT"
    skill, low = c.get("skill_vs_usual_rate"), (c.get("skill_vs_usual_rate_ci95") or [None])[0]
    if skill is None or low is None or skill <= 0:
        return "DO NOT USE"
    return "USE" if low > 0 else "UNPROVEN"


def backtest_block(model_cfg, metrics, label, variant="now", case=CASE):
    """The held-out-winter scores from train.py, for the case that matches serving: an alert at 6 AM from the
    reading for 5 to 6 AM, with current weather only."""
    a = (metrics or {}).get(variant, {}).get(case)
    if not a:
        return None
    high = model_cfg.get("bad_case", "p95")
    out = {"period": f"{model_cfg['test'][0]} to {model_cfg['test'][1]}",
           "case": case,
           "note": "From train.py on the held-out winter: alerts issued at 6 AM from the reading for 5 to 6 AM, "
                   "for the hours starting 7, 8 and 9 AM, without a weather forecast.",
           "level": {"verdict": level_verdict(metrics, model_cfg, variant, case), "skill_vs_persistence": a["skill_vs_persistence"],
                     "ci95": a.get("skill_vs_persistence_ci95"), "mae_model": a["mae_model"], "mae_persistence": a["mae_persistence"],
                     f"{high}_coverage": a.get(f"{high}_coverage"), "hours_scored": a["n"]},
           "chance": None}
    c = a.get("chance", {}).get(label)
    if c:
        out["chance"] = {"verdict": chance_verdict(metrics, label, variant, case), "label": label,
                         "skill_vs_usual_rate": c["skill_vs_usual_rate"], "ci95": c["skill_vs_usual_rate_ci95"],
                         "auc": c["auc"], "calibration_error": c["calibration_error"], "event_rate": c["event_rate"],
                         "reliability": c["reliability"]}
    return out


def score_day(store, town, model_cfg, day):
    """One morning's record. day is 'YYYY-MM-DD'. Works when there was no forecast or no readings: it says so."""
    town_id, sid = town["town_id"], town["station"]["station_id"]
    high = model_cfg.get("bad_case", "p95")
    label = alert_label(town, model_cfg)
    threshold = model_cfg["label_thresholds"][label] if label else 91.0
    readings = {}
    for hour, row in store.query(AQ, sid, start=f"{day}T00:00", end=f"{day}T23:00"):
        v = row.get("pm25")
        readings[hour] = v if v is not None and 0 < v < 1500 else None
    school = [readings.get(f"{day}T{h:02d}:00") for h in town["school_hours"]]
    school = [v for v in school if v is not None]

    morning = store.get(MORNING, town_id, day)
    hours, flagged, issued_at = [], None, None
    if morning and morning.get("forecast"):
        issued_at = morning["forecast"]["issued_at"]
        base = morning["station"]["latest"]["pm25"]
        flagged = morning["alert"]["school_flag"]["outdoor_pt"] == "move_indoors"
        for h in morning["forecast"]["hours"]:
            when = h["time"][:16]
            if when[:10] != day or int(when[11:13]) not in town["school_hours"]:
                continue
            actual = readings.get(when[:13] + ":00")
            hours.append({"time": when[11:16], "forecast_p50": h["p50"], f"forecast_{high}": h[high], "persistence": base,
                          "actual": None if actual is None else round(actual, 1),
                          "error": None if actual is None else round(abs(h["p50"] - actual), 1),
                          "persistence_error": None if actual is None else round(abs(base - actual), 1),
                          f"within_{high}": None if actual is None else actual <= h[high]})
    scored = [x for x in hours if x["actual"] is not None]
    was_bad = any(v >= threshold for v in school) if school else None
    if morning is None or flagged is None:
        outcome = "no_forecast"
    elif was_bad is None:
        outcome = "no_readings"
    else:
        outcome = OUTCOME[(flagged, was_bad)]
    return {"date": day, "issued_at": issued_at, "hours": hours, "hours_scored": len(scored),
            "mae_model": _mean([x["error"] for x in scored]), "mae_persistence": _mean([x["persistence_error"] for x in scored]),
            "alert_sent": store.get(ALERT, town_id, day) is not None, "flagged": flagged,
            "label": label, "threshold": threshold, "was_bad": was_bad, "outcome": outcome}


def build_accuracy(store, town, model_cfg, metrics, as_of, schema_version, window_days=14):
    """The accuracy panel: the latest scored morning, the last two weeks, and the backtest for comparison."""
    town_id = town["town_id"]
    high = model_cfg.get("bad_case", "p95")
    start = (Date.fromisoformat(as_of) - timedelta(days=window_days - 1)).isoformat()
    days = [doc for _, doc in store.query(CHECK, town_id, start=start, end=as_of)]
    hours = [h for d in days for h in d["hours"] if h["actual"] is not None]
    mae_model, mae_pers = _mean([h["error"] for h in hours]), _mean([h["persistence_error"] for h in hours])
    count = lambda outcome: sum(1 for d in days if d["outcome"] == outcome)
    return {
        "schema_version": schema_version, "is_sample": False, "town_id": town_id, "updated_for": as_of,
        "yesterday": days[-1] if days else None,
        "running": {"window_days": window_days, "mornings_scored": sum(1 for d in days if d["hours_scored"]),
                    "hours_scored": len(hours), "mae_model": mae_model, "mae_persistence": mae_pers,
                    "skill_vs_persistence": round(1 - mae_model / mae_pers, 2) if hours and mae_pers else None,
                    f"{high}_coverage": round(sum(1 for h in hours if h[f"within_{high}"]) / len(hours), 2) if hours else None,
                    "hits": count("hit"), "misses": count("miss"), "false_alarms": count("false_alarm"),
                    "correct_quiet": count("correct_quiet"), "no_forecast": count("no_forecast"),
                    "note": "Too few mornings to mean much until about two weeks have been scored." if len(days) < 10 else None},
        "daily": [{k: d[k] for k in ("date", "mae_model", "mae_persistence", "alert_sent", "flagged", "was_bad", "outcome")} for d in days],
        "backtest": backtest_block(model_cfg, metrics, alert_label(town, model_cfg)),
    }


def update(store, town, model_cfg, metrics, day, schema_version):
    """Scores one day and rebuilds the accuracy panel. Safe to call again for the same day."""
    record = score_day(store, town, model_cfg, day)
    store.put(CHECK, town["town_id"], day, record)
    doc = build_accuracy(store, town, model_cfg, metrics, day, schema_version)
    store.put(ACCURACY, town["town_id"], "current", doc)
    return record
