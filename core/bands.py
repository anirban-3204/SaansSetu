"""India AQI bands for PM2.5 (ug/m3). Defined officially on 24-hour averages; used here on hourly values as a guide."""

BANDS = [
    ("good", "Good", 0, 30),
    ("satisfactory", "Satisfactory", 31, 60),
    ("moderate", "Moderate", 61, 90),
    ("poor", "Poor", 91, 120),
    ("very_poor", "Very Poor", 121, 250),
    ("severe", "Severe", 251, None),
]
ORDER = [b[0] for b in BANDS]
LABEL_EN = {b[0]: b[1] for b in BANDS}
LABEL_HI = {"good": "अच्छी", "satisfactory": "संतोषजनक", "moderate": "मध्यम",
            "poor": "खराब", "very_poor": "बहुत खराब", "severe": "गंभीर"}
THRESHOLD = {b[0]: b[2] for b in BANDS}


def band(pm25):
    """Band id for a PM2.5 value, or None when the value is missing."""
    if pm25 is None or pm25 != pm25:  # None or NaN
        return None
    for bid, _, _, hi in BANDS:
        if hi is None or pm25 < hi + 0.5:
            return bid
    return "severe"


def at_least(band_id, floor):
    """True when band_id is floor or worse."""
    return band_id is not None and ORDER.index(band_id) >= ORDER.index(floor)
