"""Route exposure: spreads the station forecast onto each street segment.

    segment = station x (1 + plume + traffic)

plume    larger when the segment is downwind of the plant, fading with distance
traffic  a rush-hour bump, larger on main roads

This is a transparent estimate with visible settings, not a trained model and not a measurement.
"""
import math

from .geo import minutes


def station_curve(points):
    """points: [(minute_of_day, pm25), ...] in time order. Returns a function minute -> pm25 that
    joins the points with straight lines and holds the first and last values outside them."""
    points = sorted(points)
    if not points:
        raise ValueError("no points for the station curve")

    def curve(minute):
        if minute <= points[0][0]:
            return points[0][1]
        for (m0, v0), (m1, v1) in zip(points, points[1:]):
            if minute <= m1:
                return v0 + (v1 - v0) * (minute - m0) / (m1 - m0)
        return points[-1][1]
    return curve


def plume_factor(segment, wind_from_deg, params):
    """0 when the segment is upwind or crosswind of the plant, up to plume_strength when directly downwind and close."""
    if wind_from_deg is None:
        return 0.0
    wind_to = (wind_from_deg + 180.0) % 360.0
    align = max(0.0, math.cos(math.radians(segment["bearing_from_plant_deg"] - wind_to)))
    return params["plume_strength"] * align * math.exp(-segment["distance_to_plant_m"] / params["plume_decay_m"])


def traffic_factor(segment, minute, params):
    peak, width = minutes(params["traffic_peak"]), params["traffic_width_minutes"]
    strength = params["traffic_strength"].get(segment["road"], 0.0)
    return strength * math.exp(-((minute - peak) / width) ** 2)


def segment_factor(segment, minute, wind_from_deg, params):
    return 1.0 + plume_factor(segment, wind_from_deg, params) + traffic_factor(segment, minute, params)


def segment_pm25(segment, minute, curve, wind_from_deg, params):
    return curve(minute) * segment_factor(segment, minute, wind_from_deg, params)
