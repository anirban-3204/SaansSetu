"""Evidence: from which wind direction do the dirtiest winter mornings arrive at the station?

A conditional probability table (the CBPF method): for each wind direction and speed, the share of hours that
were among the dirtiest. Plain counting on the station archive. No model, and no claim about any source:
one station plus area wind shows a direction, not proof.

    station_stats(frame)      the counting, from the hourly archive frame (train.load). Needs the full archive,
                              so make_evidence.py runs it once and saves the result beside the models.
    describe(stats, town)     adds the town's plant direction and the sentences the dashboard shows.
"""
import math

import numpy as np
import pandas as pd

from . import READING_MINUTES
from .bands import band
from .geo import bearing_deg

DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]      # wind FROM these, 45 degrees each, centred on the compass point
SPEED_EDGES = [0, 1, 2, 3, 4, 6]                           # m/s. The last band also takes anything faster.
CALM_BELOW = 1.0                                           # under this the air is nearly still and direction means little
MONTHS, HOURS = [10, 11, 12, 1, 2], [6, 7, 8, 9]           # winter; the hours starting 6 to 9 AM
TOP_SHARE = 0.25                                           # "dirtiest" = the top quarter, judged against the same month and hour
MIN_HOURS, MIN_HIGH, MIN_CELL = 300, 50, 10
EN = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east", "S": "south", "SW": "south-west", "W": "west", "NW": "north-west"}
HI = {"N": "उत्तर", "NE": "उत्तर-पूर्व", "E": "पूर्व", "SE": "दक्षिण-पूर्व", "S": "दक्षिण", "SW": "दक्षिण-पश्चिम", "W": "पश्चिम", "NW": "उत्तर-पश्चिम"}


def hourly_wind(frame):
    """(speed m/s, direction it blows FROM in degrees) for each hour. A PM2.5 value covers a whole hour, so the
    wind paired with it is the vector mean of the wind at the hour's start and at its end."""
    idx = pd.date_range(frame.index.min(), frame.index.max(), freq="h")
    ws, wd = frame["wind_speed_10m"].reindex(idx), np.deg2rad(frame["wind_direction_10m"].reindex(idx))
    u, v = -ws * np.sin(wd), -ws * np.cos(wd)             # where the air is going: east and north parts
    steps = READING_MINUTES // 60
    u = pd.concat([u, u.shift(-steps)], axis=1).mean(axis=1, skipna=True).where(u.notna())
    v = pd.concat([v, v.shift(-steps)], axis=1).mean(axis=1, skipna=True).where(v.notna())
    speed = np.hypot(u, v)
    return speed, (np.degrees(np.arctan2(-u, -v)) + 360) % 360


def _pairs(all_by_day, high_by_day):
    """For every pair of neighbouring directions (a 90-degree sector): hours, dirtiest hours, and how many times
    more often the dirtiest hours came from there than the wind blew from there."""
    a = all_by_day.sum(axis=0)
    h = high_by_day.sum(axis=0)
    a2, h2 = a + np.roll(a, -1), h + np.roll(h, -1)
    with np.errstate(divide="ignore", invalid="ignore"):
        lift = (h2 / h.sum()) / (a2 / a.sum())
    ok = (a2 >= MIN_HOURS / 10) & (a2 / max(a.sum(), 1) >= 0.05)   # ignore sectors the wind almost never blows from
    return a2, h2, np.where(ok & np.isfinite(lift), lift, -np.inf)


def station_stats(frame, station_id=None, seed=7, resamples=400):
    """The table and the one sector that stands out, if any does. Returns a plain dict."""
    need = {"pm25", "wind_speed_10m", "wind_direction_10m"}
    out = {"station_id": station_id, "verdict": "not_enough_data", "top_sector": None,
           "wind_source": "Weather reanalysis (ERA5 via Open-Meteo) for the area around the station. Not measured at the station."}
    if not need <= set(frame.columns):
        return {**out, "why": "the archive has no wind for this station"}
    speed, from_deg = hourly_wind(frame)
    d = pd.DataFrame({"pm": frame["pm25"], "speed": speed, "deg": from_deg}).dropna()
    d = d[d.index.month.isin(MONTHS) & d.index.hour.isin(HOURS)]
    out["period"] = {"from": str(d.index.min().date()) if len(d) else None, "to": str(d.index.max().date()) if len(d) else None,
                     "months": "October to February", "hours": "06:00 to 10:00", "hours_used": int(len(d)),
                     "days_used": int(d.index.normalize().nunique())}
    if len(d) < MIN_HOURS:
        return {**out, "why": f"only {len(d)} winter-morning hours with both PM2.5 and wind (needs {MIN_HOURS})"}

    # "Dirtiest" is judged against the same month and the same hour of the morning. January is dirtier than October
    # and 8 AM dirtier than 6 AM whatever the wind does; comparing like with like keeps the calendar out of the answer.
    cut = d.groupby([d.index.month, d.index.hour])["pm"].transform(lambda s: s.quantile(1 - TOP_SHARE))
    high = (d["pm"] >= cut).to_numpy()
    dir_bin = (((d["deg"].to_numpy() + 22.5) % 360) // 45).astype(int)
    speed_bin = np.clip(np.searchsorted(SPEED_EDGES, d["speed"].to_numpy(), side="right") - 1, 0, len(SPEED_EDGES) - 2)
    rows = len(SPEED_EDGES) - 1
    count = np.zeros((rows, 8), dtype=int)
    dirty = np.zeros((rows, 8), dtype=int)
    np.add.at(count, (speed_bin, dir_bin), 1)
    np.add.at(dirty, (speed_bin, dir_bin), high.astype(int))
    typical = float(cut.median())
    out["threshold"] = {"rule": "the dirtiest quarter of hours, compared with the same month and the same hour of the morning",
                        "pm25_typical": round(typical, 1), "pm25_lowest": round(float(cut.min()), 1),
                        "pm25_highest": round(float(cut.max()), 1), "band_typical": band(typical)}
    out["polar"] = {
        "wind_from_labels": DIRS, "wind_from_deg_bins": [k * 45 for k in range(8)], "bin_width_deg": 45,
        "wind_speed_ms_edges": SPEED_EDGES,
        "probability": [[None if c == 0 else round(h / c, 2) for h, c in zip(hr, cr)] for hr, cr in zip(dirty.tolist(), count.tolist())],
        "count": count.tolist(), "min_count": MIN_CELL,
        "how_to_read": "probability[speed band][direction] = share of hours in that cell that were among the dirtiest. "
                       "Draw as a polar heatmap: angle = where the wind came from, radius = wind speed. "
                       "Directions are centred on the compass point. Grey out cells with count below min_count.",
    }
    calm = d["speed"].to_numpy() < CALM_BELOW
    out["calm"] = {"under_ms": CALM_BELOW, "share_of_high_hours": round(float(calm[high].mean()), 2) if high.any() else None,
                   "share_of_all_hours": round(float(calm.mean()), 2)}

    # The sector is judged on moving air only, day by day, so days can be resampled whole (hours within a day move together).
    moving = ~calm
    days, day_of = np.unique(d.index.normalize()[moving], return_inverse=True)
    all_by_day = np.zeros((len(days), 8))
    high_by_day = np.zeros((len(days), 8))
    np.add.at(all_by_day, (day_of, dir_bin[moving]), 1)
    np.add.at(high_by_day, (day_of, dir_bin[moving]), high[moving].astype(float))
    if high_by_day.sum() < MIN_HIGH or all_by_day.sum() < MIN_HOURS:
        if out["calm"]["share_of_high_hours"] >= 0.5:      # plenty of data, but the dirty hours are the still ones
            return {**out, "verdict": "mostly_still_air", "why": "most of the dirtiest hours came in nearly still air, which has no direction"}
        return {**out, "why": "too few hours with moving air to judge a direction"}
    a2, h2, lift = _pairs(all_by_day, high_by_day)
    k = int(np.argmax(lift))
    if not np.isfinite(lift[k]):
        return {**out, "verdict": "no_clear_direction", "why": "the wind hardly ever blows from any one side for long enough to judge"}
    rng = np.random.default_rng(seed)
    lifts, same = [], 0
    for _ in range(resamples):
        pick = rng.integers(0, len(days), len(days))
        _, _, again = _pairs(all_by_day[pick], high_by_day[pick])
        if np.isfinite(again[k]):
            lifts.append(float(again[k]))
        same += int(min((int(np.argmax(again)) - k) % 8, (k - int(np.argmax(again))) % 8) <= 1)
    low, top = (float(np.percentile(lifts, 2.5)), float(np.percentile(lifts, 97.5))) if lifts else (0.0, 0.0)
    stable = same / resamples
    out["top_sector"] = {
        "wind_from": [DIRS[k], DIRS[(k + 1) % 8]], "wind_from_deg": [(k * 45 - 22.5) % 360, (k * 45 + 67.5) % 360],
        "centre_deg": (k * 45 + 22.5) % 360, "hours": int(a2[k]),
        "share_of_high_hours": round(float(h2[k] / high_by_day.sum()), 2), "share_of_all_hours": round(float(a2[k] / all_by_day.sum()), 2),
        "probability": round(float(h2[k] / a2[k]), 2), "lift": round(float(lift[k]), 2), "lift_ci95": [round(low, 2), round(top, 2)],
        "stable_in": round(stable, 2),
        "note": "lift = how many times more often the dirtiest hours came from this sector than the wind blew from it. "
                "stable_in = share of day-resamples in which the same or a neighbouring sector came out on top.",
    }
    out["verdict"] = "stands_out" if low >= 1.2 and stable >= 0.8 else "no_clear_direction"
    out["verdict_rule"] = ("stands_out needs the lower end of lift_ci95 at 1.2 or more and stable_in at 0.8 or more. "
                           "Other verdicts: no_clear_direction, mostly_still_air, not_enough_data.")
    return out


def describe(stats, town, schema_version):
    """The document the dashboard reads: the station table, the plant's direction if the town has a real one,
    and fixed sentences in English and Hindi. No company is ever named."""
    doc = {"schema_version": schema_version, "is_sample": False, "town_id": town["town_id"],
           "question": "From which wind direction do the dirtiest winter mornings arrive at the station?", **stats}
    sector, plant = stats.get("top_sector"), town.get("plant")
    doc["plant"] = None
    if plant and "lat" in town["station"] and "lat" in plant:
        toward = bearing_deg((town["station"]["lat"], town["station"]["lon"]), (plant["lat"], plant["lon"]))
        placeholder = bool(plant.get("approximate") or plant.get("placeholder"))
        inside = None
        if sector:
            inside = abs((toward - sector["centre_deg"] + 180) % 360 - 180) <= 45
        doc["plant"] = {"bearing_from_station_deg": round(toward), "label": DIRS[int(((toward + 22.5) % 360) // 45)],
                        "location_is_placeholder": placeholder, "inside_top_sector": inside}

    if stats["verdict"] == "not_enough_data":
        en = "There are too few winter-morning hours with wind data to say anything about direction."
        hi = "सर्दियों की सुबह के इतने कम घंटों का हवा का डेटा है कि दिशा के बारे में कुछ नहीं कहा जा सकता।"
    elif stats["verdict"] == "mostly_still_air":
        en = "The dirtiest winter mornings here are mostly still-air mornings, so they point in no direction."
        hi = "यहाँ सर्दियों की सबसे प्रदूषित सुबहें ज़्यादातर थमी हुई हवा वाली होती हैं, इसलिए वे किसी दिशा की ओर इशारा नहीं करतीं।"
    elif stats["verdict"] == "no_clear_direction":
        en = "No wind direction stands out on winter mornings. The dirtiest hours came from all sides about as often as the wind did."
        hi = "सर्दियों की सुबह हवा की कोई एक दिशा अलग नहीं दिखती। सबसे प्रदूषित घंटे हर दिशा से लगभग उतनी ही बार आए जितनी बार हवा चली।"
    else:
        a, b = sector["wind_from"]
        p, q = round(sector["share_of_high_hours"] * 100), round(sector["share_of_all_hours"] * 100)
        en = (f"On winter mornings with moving air, {p}% of the dirtiest hours came with wind from the {EN[a]} to {EN[b]}. "
              f"The wind blows from there {q}% of the time.")
        hi = (f"सर्दियों की जिन सुबहों में हवा चल रही थी, उनमें सबसे प्रदूषित घंटों में से {p}% में हवा {HI[a]} से {HI[b]} की ओर से आई। "
              f"उस ओर से हवा {q}% समय चलती है।")
        if doc["plant"] and not doc["plant"]["location_is_placeholder"]:
            if doc["plant"]["inside_top_sector"]:
                en += " Seen from the station, that is the side the industrial area lies on."
                hi += " स्टेशन से देखने पर औद्योगिक क्षेत्र उसी ओर है।"
            else:
                en += " Seen from the station, the industrial area does not lie on that side."
                hi += " स्टेशन से देखने पर औद्योगिक क्षेत्र उस ओर नहीं है।"
    calm = stats.get("calm")
    if calm and calm["share_of_high_hours"] is not None and stats["verdict"] != "not_enough_data":
        c = round(calm["share_of_high_hours"] * 100)
        en += f" {c}% of the dirtiest hours came in nearly still air, when pollution stays close to where it is made."
        hi += f" सबसे प्रदूषित घंटों में से {c}% लगभग थमी हुई हवा में आए, जब प्रदूषण वहीं ठहरा रहता है जहाँ बनता है।"
    doc["summary"] = {"en": en, "hi": hi}
    doc["caveats"] = {
        "en": ["One station and area wind show a direction, not proof of a source.",
               "The wind is from a weather reanalysis for the area, not from an instrument at the station."]
              + (["The plant location in this town's file is a placeholder, so nothing is said about it."]
                 if doc["plant"] and doc["plant"]["location_is_placeholder"] else []),
        "hi": ["एक स्टेशन और क्षेत्र की हवा से दिशा पता चलती है, स्रोत का प्रमाण नहीं।",
               "हवा का डेटा क्षेत्र के मौसम पुनर्विश्लेषण से है, स्टेशन पर लगे यंत्र से नहीं।"]
              + (["इस शहर की फ़ाइल में संयंत्र की जगह अनुमानित है, इसलिए उसके बारे में कुछ नहीं कहा गया है।"]
                 if doc["plant"] and doc["plant"]["location_is_placeholder"] else []),
    }
    return _plain(doc)


def _plain(x):
    """numpy numbers to plain ones, NaN to null, so the document is valid JSON."""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if math.isnan(x) or math.isinf(x) else float(x)
    return x
