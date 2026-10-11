"""Small geometry helpers. Points are (lat, lon) in degrees."""
import math

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def distance_m(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bearing_deg(a, b):
    """Compass bearing from a to b: 0 = north, 90 = east."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    return COMPASS[int((deg % 360 + 11.25) // 22.5) % 16]


def hhmm(minute):
    minute = int(round(minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def minutes(clock):
    """'07:45' -> 465."""
    h, m = clock.split(":")
    return int(h) * 60 + int(m)
