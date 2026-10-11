"""One run of SaansSetu: station history + trained models + town file -> the run document the dashboard reads.

    ingest -> forecast -> exposure -> departure -> alert

run_once() has no AWS in it. The Lambda, the replay tool and the tests all call this same function.

A town without routes stops after the forecast: it gets the forecast and the school flag, and its
exposure and departure steps are marked skipped.
"""
import pandas as pd

from . import READING_MINUTES, SCHEMA_VERSION
from . import alert as alerts
from . import departure, exposure, forecasting
from .bands import at_least, band
from .geo import compass, hhmm, minutes
from .townconfig import alert_label

POLLUTANTS = ["pm25", "pm10", "no2", "so2", "co"]
READING = pd.Timedelta(minutes=READING_MINUTES)


def _num(v, nd=1):
    return None if v is None or v != v else round(float(v), nd)


def _compass(deg):
    return compass(deg) if deg is not None else None


def _start(town, now, mode, replay_of):
    tz = town.get("timezone_offset", "+05:30")
    iso = lambda t: pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M") + tz
    run = {
        "schema_version": SCHEMA_VERSION, "is_sample": False, "town_id": town["town_id"],
        "run_id": f"{town['town_id']}-{iso(now)}", "mode": mode, "has_routes": bool(town.get("has_routes")),
        "clock": {"now": iso(now), "replay_of": replay_of, "replay_speed": None},
        "status": "ok", "status_message": None,
    }
    return run, iso


BLANK = {"slots": [], "routes": [], "departures": None}


def failed_run(town, now, mode, detail, step="ingest", replay_of=None):
    """The run document for a run that could not finish. The dashboard shows it instead of going blank."""
    run, _ = _start(town, pd.Timestamp(now), mode, replay_of)
    order = ["ingest", "forecast", "exposure", "departure", "alert"]
    steps = [{"step": s, "status": "ok", "detail": None} for s in order[:order.index(step)]]
    steps.append({"step": step, "status": "failed", "detail": detail})
    run.update(status="failed", status_message={"en": "This run could not finish. There is no forecast until the next run succeeds.",
                                                "hi": "यह रन पूरा नहीं हो सका। अगला रन सफल होने तक पूर्वानुमान उपलब्ध नहीं है।"},
               station=None, weather=None, forecast=None, steps=steps,
               alert=alerts.held("run_failed", detail), **BLANK)
    return run


def station_points(reading_at, latest, fc, day):
    """[(minute of the day, PM2.5)] for the station curve: the latest reading, then each forecast hour.
    Every hourly value is an average over the hour starting at its stamp, so it sits at the middle of that hour."""
    middle = lambda t: int((t - day).total_seconds() // 60) + READING_MINUTES // 2
    return [(middle(reading_at), latest)] + [(middle(t), f["p50"]) for t, f in fc.items()]


def run_once(town, history, model_cfg, model_dir, now, mode="live", variant="now",
             already_sent_today=False, recipients=0, replay_of=None):
    """
    town        prepared town file (townconfig.load_town)
    history     hourly frame indexed by naive IST time: pm25 (+ pm10, no2, so2, co) and weather columns
    model_cfg   the station's config.json written by train.py
    model_dir   folder holding <variant>/ model files and calibration.json (e.g. out/site_1418/final)
    now         the clock time of the run (naive IST). A reading stamped T covers T to T + 60 min, so only
                readings whose hour has ended by now are used. Nothing later is touched.
    """
    now = pd.Timestamp(now)
    run, iso = _start(town, now, mode, replay_of)

    # ---------------------------------------------------------------- ingest
    seen = history.loc[:now - READING]      # a reading exists only once the hour it covers is over
    pm = seen["pm25"].dropna() if "pm25" in seen else pd.Series(dtype=float)
    if pm.empty:
        run.update(status="failed", status_message={"en": "No station readings available.", "hi": "स्टेशन का कोई डेटा उपलब्ध नहीं है।"},
                   station=None, weather=None, forecast=None,
                   steps=[{"step": "ingest", "status": "failed", "detail": "No PM2.5 readings up to now"}],
                   alert=alerts.held("no_data", "No station readings available."), **BLANK)
        return run
    reading_at, latest = pm.index[-1], float(pm.iloc[-1])
    age = int((now - (reading_at + READING)).total_seconds() // 60)    # minutes since that hour ended
    stale = age > town["stale_after_minutes"]
    row = seen.loc[reading_at]
    last24 = seen["pm25"].reindex(pd.date_range(reading_at - pd.Timedelta(hours=23), reading_at, freq="h"))
    avg24 = _num(last24.mean())
    run["station"] = {
        "latest": {"time": iso(reading_at), **{c: _num(row.get(c)) for c in POLLUTANTS if c in seen}, "band": band(latest)},
        "age_minutes": age, "is_stale": stale, "stale_after_minutes": town["stale_after_minutes"],
        "reading_minutes": READING_MINUTES,
        "reading_note": "Each hourly value is the average over the hour that starts at its time. age_minutes counts from the end of that hour.",
        "official_24h": {"pm25_avg": avg24, "band": band(avg24), "hours_with_data": int(last24.notna().sum()),
                         "note": "What the public city AQI badge reflects: a 24-hour average"},
        "history_24h": [{"time": iso(t), "pm25": _num(v), "band": band(_num(v))} for t, v in last24.items()],
    }
    wind_from = _num(row.get("wind_direction_10m"))
    run["weather"] = {
        "now": {"time": iso(reading_at), "wind_speed_ms": _num(row.get("wind_speed_10m")), "wind_from_deg": wind_from,
                "wind_from": _compass(wind_from),
                "mixing_height_m": _num(row.get("boundary_layer_height"), 0), "temperature_c": _num(row.get("temperature_2m")),
                "humidity_pct": _num(row.get("relative_humidity_2m"), 0)},
        "forecast_available": variant == "fut", "forecast": [],
    }
    steps = [{"step": "ingest", "status": "stale" if stale else "ok", "detail": f"Latest reading covers {reading_at:%H:%M} to {reading_at + READING:%H:%M}, {age} min old"}]

    # ---------------------------------------------------------------- forecast
    fc, high = forecasting.forecast(history, model_cfg, model_dir, reading_at, variant)
    labels = model_cfg["labels"]
    hours = []
    for t, f in fc.items():
        hours.append({"time": iso(t), "horizon_h": int((t - reading_at).total_seconds() // 3600), "p50": f["p50"], high: f[high],
                      "band_p50": band(f["p50"]), f"band_{high}": band(f[high]),
                      "chance": {name: f[f"chance_{name}"] for name in labels}})
    run["forecast"] = {
        "issued_at": iso(now), "based_on_reading_at": iso(reading_at), "feature_set": variant, "bad_case": high,
        "feature_set_note": "fut = used the weather forecast; now = used current weather only",
        "hours": hours,
    }
    if variant == "fut":
        fut = history.loc[reading_at + pd.Timedelta(hours=1): reading_at + pd.Timedelta(hours=max(model_cfg["horizons"]))]
        run["weather"]["forecast"] = [
            {"time": iso(t), "wind_speed_ms": _num(r.get("wind_speed_10m")), "wind_from_deg": _num(r.get("wind_direction_10m")),
             "wind_from": _compass(_num(r.get("wind_direction_10m"))),
             "mixing_height_m": _num(r.get("boundary_layer_height"), 0)} for t, r in fut.iterrows()]

    day = now.normalize()
    school_start = day + pd.Timedelta(minutes=minutes(town["school"]["starts_at"]))
    now_minute = now.hour * 60 + now.minute
    covers = max(fc) >= school_start and reading_at >= day
    school_fc = {t: f for t, f in fc.items() if t.normalize() == day and t.hour in town["school_hours"]}
    steps.append({"step": "forecast", "status": "ok" if covers else "partial",
                  "detail": f"{'With' if variant == 'fut' else 'Without'} weather forecast; covers {len(school_fc)} of {len(town['school_hours'])} school hours"})

    # The school flag depends only on the forecast for school hours, so every town gets one.
    label = alert_label(town, model_cfg)
    if label and school_fc:
        school_chance = max(f[f"chance_{label}"] for f in school_fc.values())
        bad = school_chance >= town["alert_rule"]["min_chance"]
    else:
        school_chance = None
        bad = any(at_least(band(f["p50"]), "poor") for f in school_fc.values())
    flag = alerts.school_flag(label, school_chance, bad) if school_fc and covers and not stale else None

    def stop(code, reason, message, degraded=False):
        run.update(status="degraded" if degraded else run["status"], status_message=message,
                   steps=steps + [{"step": "exposure", "status": "skipped", "detail": reason},
                                  {"step": "departure", "status": "skipped", "detail": None},
                                  {"step": "alert", "status": "held", "detail": code}],
                   alert=alerts.held(code, reason, flag), **BLANK)
        return run

    if stale:
        return stop("stale_data", "Latest station reading is too old to forecast the school window.",
                    {"en": f"Station feed is {age // 60} h {age % 60} min behind. No route advice this morning.",
                     "hi": f"स्टेशन का डेटा {age // 60} घंटे {age % 60} मिनट पुराना है। आज सुबह रास्ते की सलाह उपलब्ध नहीं है।"},
                    degraded=True)
    if not town["has_routes"]:
        reason = "No routes mapped for this town"
        run.update(steps=steps + [{"step": "exposure", "status": "skipped", "detail": reason},
                                  {"step": "departure", "status": "skipped", "detail": None},
                                  {"step": "alert", "status": "not_needed", "detail": "no_routes"}],
                   alert=alerts.no_routes(town, label, flag), **BLANK)
        return run
    if now_minute > minutes(town["departure_window"]["latest"]):
        return stop("after_departure_window", "The morning departure window has passed.", None)
    if not covers:
        return stop("before_morning_window", "The forecast does not reach school time yet.", None)

    # ---------------------------------------------------------------- exposure
    curve = exposure.station_curve(station_points(reading_at, latest, fc, day))
    params = town["exposure_params"]
    pm25_at = lambda seg, minute: exposure.segment_pm25(seg, minute, curve, wind_from, params)
    s = town["slots"]
    slots = list(range(minutes(s["start"]), minutes(s["end"]) + 1, s["step_minutes"]))
    names = {r["route_id"]: r["name"] for r in town["routes"]}
    routes_out = []
    for rid, segs in town["_segments"].items():
        seg_out = [{"segment_id": g["segment_id"], "pm25": [int(round(pm25_at(g, m))) for m in slots]} for g in segs]
        for so in seg_out:
            so["band"] = [band(v) for v in so["pm25"]]
        total = sum(g["length_m"] for g in segs)
        worst = [max(so["pm25"][k] for so in seg_out) for k in range(len(slots))]
        mean = [int(round(sum(so["pm25"][k] * g["length_m"] for so, g in zip(seg_out, segs)) / total)) for k in range(len(slots))]
        k = max(range(len(slots)), key=lambda i: worst[i])
        routes_out.append({"route_id": rid, "name": names[rid],
                           "summary": {"mean_pm25": mean, "worst_pm25": worst, "worst_band": [band(v) for v in worst],
                                       "peak": {"time": hhmm(slots[k]), "pm25": worst[k], "band": band(worst[k])}},
                           "segments": seg_out})
    steps.append({"step": "exposure", "status": "ok" if wind_from is not None else "partial",
                  "detail": f"{sum(len(v) for v in town['_segments'].values())} segments x {len(slots)} time slots"
                            + ("" if wind_from is not None else "; no wind reading, plume not applied")})

    # ---------------------------------------------------------------- departure
    dep = departure.compare(town, pm25_at, not_before_minute=now_minute)
    steps.append({"step": "departure", "status": "ok", "detail": f"{len(dep['options'])} options compared"})

    # ---------------------------------------------------------------- alert
    usual_peak = next(r for r in routes_out if r["route_id"] == dep["usual"]["route_id"])["summary"]["peak"]
    if school_chance is None:   # no chance model for this town: judge the morning by the usual route's worst band
        bad = at_least(usual_peak["band"], "poor")
    block = alerts.decide(town, dep, usual_peak, label, school_chance, bad, already_sent_today, iso(now), recipients)
    block = alerts.not_before(block, now_minute)
    steps.append({"step": "alert", "status": "sent" if block["decision"] == "send" else "not_needed", "detail": block["reason_code"]})

    run.update(steps=steps, slots=[hhmm(m) for m in slots], routes=routes_out, departures=dep, alert=block)
    return run
