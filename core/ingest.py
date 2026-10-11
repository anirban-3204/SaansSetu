"""Getting readings in: a source hands over hourly rows, sync() stores them, history() rebuilds the frame the model reads.

A source has a name and two methods, each returning an hourly frame indexed by naive IST time
holding rows stamped after `since` and up to `until`:

    pollutants(station, since, until)   columns from pm25, pm10, no2, co, so2
    weather(station, since, until)      the Open-Meteo columns train.py reads

ArchiveSource reads the files fetch_data.py made and never reveals a reading before the hour it covers has
ended, so a replay runs the live code on a past morning and knows only what was knowable then. LiveSource pairs a live pollutant feed with live weather.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

import train
from . import READING_MINUTES, forecasting
from .store import AQ, WX, READING_TTL_DAYS

POLLUTANTS = ["pm25", "pm10", "no2", "co", "so2"]
WX_COLS = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "wind_speed_10m", "wind_direction_10m",
           "precipitation", "surface_pressure", "cloud_cover", "boundary_layer_height"]
HOUR = "%Y-%m-%dT%H:00"
KEEP_HOURS = forecasting.HISTORY_HOURS + 24   # what one forecast can look back on, plus a day of margin
OVERLAP_HOURS = 6                             # re-read the last few hours each time: feeds fill in late


def rows_of(frame):
    """{hour: {column: value}} for storing. Missing values become null; hours with nothing at all are dropped."""
    out = {}
    for t, row in frame.iterrows():
        doc = {c: (None if v != v else float(v)) for c, v in row.items()}
        if any(v is not None for v in doc.values()):
            out[pd.Timestamp(t).strftime(HOUR)] = doc
    return out


def _between(frame, since, until):
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame[(frame.index > pd.Timestamp(since)) & (frame.index <= pd.Timestamp(until))]


class ArchiveSource:
    """The fetch_data.py files, read as if they were a feed. A reading stamped 06:00 covers 06:00 to 07:00, so it
    is handed over from 07:00, never earlier. lag_minutes adds a publishing delay on top, the way a slow feed would."""
    name = "archive"

    def __init__(self, data_dir, lag_minutes=0):
        self.dir, self.lag = Path(data_dir), pd.Timedelta(minutes=READING_MINUTES + lag_minutes)
        self._cache = {}

    def _read(self, path):
        if path not in self._cache:
            self._cache[path] = pd.read_csv(path, parse_dates=["time"], index_col="time")
        return self._cache[path]

    def pollutants(self, station, since, until):
        path = self.dir / f"aq_{station['station_id']}.csv.gz"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found")
        aq = self._read(path)
        aq = aq.groupby(aq.index.floor("h")).mean()   # same hourly averaging as train.load
        return _between(aq[[c for c in POLLUTANTS if c in aq]], since, pd.Timestamp(until) - self.lag)

    def weather(self, station, since, until):
        path = train.find_weather(self.dir, station["station_id"])
        if path is None:
            raise FileNotFoundError(f"no weather file for {station['station_id']}")
        wx = self._read(path)
        return _between(wx[[c for c in WX_COLS if c in wx]], since, until)


def http_json(url, params=None, headers=None, timeout=20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "saanssetu/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class OpenMeteoWeather:
    """Current weather from the Open-Meteo forecast API, on the same 0.1-degree cell and in the same units
    fetch_data.py used for training. Training weather is ERA5; this is a forecast model, so values differ a little."""
    name = "open-meteo"
    URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, get_json=http_json):
        self.get_json, self.vars = get_json, list(WX_COLS)

    def weather(self, station, since, until):
        days = int((pd.Timestamp(until) - pd.Timestamp(since)).total_seconds() // 86400) + 2
        params = {"latitude": round(float(station["lat"]), 1), "longitude": round(float(station["lon"]), 1),
                  "timezone": "Asia/Kolkata", "wind_speed_unit": "ms", "past_days": min(days, 92), "forecast_days": 1}
        try:
            js = self.get_json(self.URL, {**params, "hourly": ",".join(self.vars)})
        except urllib.error.HTTPError as e:
            if e.code != 400 or "boundary_layer_height" not in self.vars:
                raise
            self.vars.remove("boundary_layer_height")   # not offered: carry on without mixing height
            js = self.get_json(self.URL, {**params, "hourly": ",".join(self.vars)})
        hourly = js.get("hourly") or {}
        if not hourly.get("time"):
            raise ValueError(f"Open-Meteo returned no hourly data: {str(js)[:200]}")
        frame = pd.DataFrame(hourly)
        frame["time"] = pd.to_datetime(frame["time"])
        return _between(frame.set_index("time")[[c for c in WX_COLS if c in frame]], since, until)


class LiveSource:
    """A live pollutant feed paired with live weather."""
    def __init__(self, pollutant_feed, weather_feed):
        self._aq, self._wx = pollutant_feed, weather_feed
        self.name = f"{pollutant_feed.name}+{weather_feed.name}"

    def pollutants(self, station, since, until):
        return self._aq.pollutants(station, since, until)

    def weather(self, station, since, until):
        return self._wx.weather(station, since, until)


def sync(store, source, town, now):
    """Stores what the source has that the store does not: everything in reach on the first run, then only the
    newest hours. A feed that fails is reported and never raises: the run goes on with what is already stored."""
    now, station = pd.Timestamp(now), town["station"]
    sid, report = station["station_id"], {"source": source.name}
    for kind, fetch in ((AQ, source.pollutants), (WX, source.weather)):
        try:
            last = store.query(kind, sid, newest_first=True, limit=1)
            since = now.floor("h") - pd.Timedelta(hours=KEEP_HOURS + 1)   # history() reads back to exactly KEEP_HOURS
            if last:
                since = max(since, pd.Timestamp(last[0][0]) - pd.Timedelta(hours=OVERLAP_HOURS))
            rows = rows_of(fetch(station, since, now))
            if rows:
                store.put_many(kind, sid, rows, ttl_days=READING_TTL_DAYS)
            report[kind] = {"new_rows": len(rows), "error": None}
        except Exception as e:   # one bad feed must not stop the run
            report[kind] = {"new_rows": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return report


def history(store, town, now):
    """The hourly frame run_once() reads, rebuilt from the store and cleaned exactly as train.load cleans it.
    Every pollutant and weather column is present, so a feed that lacks one gives the model a gap, not an error."""
    end = pd.Timestamp(now).floor("h")
    start = end - pd.Timedelta(hours=KEEP_HOURS)
    idx = pd.date_range(start, end, freq="h")
    sid = town["station"]["station_id"]

    def frame(kind, cols):
        rows = store.query(kind, sid, start=start.strftime(HOUR), end=end.strftime(HOUR))
        f = pd.DataFrame.from_dict({pd.Timestamp(k): v for k, v in rows}, orient="index", dtype=float)
        return f.reindex(index=idx, columns=cols).astype(float)

    return train.clean(frame(AQ, POLLUTANTS)).join(frame(WX, WX_COLS))
