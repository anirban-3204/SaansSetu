"""Compares every route and leaving time by dose: concentration x minutes, summed along the trip."""
from .bands import band
from .geo import hhmm, minutes


def trip(route_id, segments, leave_minute, speed_kmh, pm25_at, school_start_minute):
    """pm25_at(segment, minute) -> ug/m3. Concentration is sampled at the middle of the time spent on each segment."""
    t, dose, peak = float(leave_minute), 0.0, 0.0
    for seg in segments:
        spent = seg["length_m"] / 1000.0 / speed_kmh * 60.0
        c = pm25_at(seg, t + spent / 2.0)
        dose += c * spent
        peak = max(peak, c)
        t += spent
    took = t - leave_minute
    return {
        "route_id": route_id, "leave": hhmm(leave_minute), "arrive": hhmm(t), "minutes": int(round(took)),
        "dose": int(round(dose)), "avg_pm25": int(round(dose / took)), "peak_pm25": int(round(peak)),
        "peak_band": band(round(peak)), "arrives_on_time": t <= school_start_minute + 1e-9,
    }


def compare(town, pm25_at, not_before_minute=None):
    """Returns the 'departures' block: every option, the usual one, the best one and the saving."""
    speed = town["travel"]["speed_kmh"]
    start = minutes(town["school"]["starts_at"])
    win = town["departure_window"]
    first, last, step = minutes(win["earliest"]), minutes(win["latest"]), win["step_minutes"]
    if not_before_minute is not None and not_before_minute > first:  # never advise leaving in the past
        first += -(-(not_before_minute - first) // step) * step
    leaves = list(range(first, last + 1, step))

    options = [trip(rid, segs, m, speed, pm25_at, start) for rid, segs in town["_segments"].items() for m in leaves]
    usual_cfg = town["usual_departure"]
    usual = trip(usual_cfg["route_id"], town["_segments"][usual_cfg["route_id"]], minutes(usual_cfg["leave"]), speed, pm25_at, start)
    on_time = [o for o in options if o["arrives_on_time"]]
    best = min(on_time, key=lambda o: (o["dose"], o["leave"])) if on_time else None

    saving = route_only = None
    if best and usual["dose"] > 0:
        saving = int(round((1 - best["dose"] / usual["dose"]) * 100))
        if best["route_id"] != usual["route_id"]:
            same_time = trip(best["route_id"], town["_segments"][best["route_id"]], minutes(usual_cfg["leave"]), speed, pm25_at, start)
            route_only = int(round((1 - same_time["dose"] / usual["dose"]) * 100))
        else:
            route_only = 0
    return {"school_starts_at": town["school"]["starts_at"], "dose_unit": "ug/m3 x minutes", "options": options,
            "usual": usual, "best": best, "saving_vs_usual_pct": saving, "saving_from_route_change_only_pct": route_only}
