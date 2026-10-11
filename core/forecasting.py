"""Serving-side forecast. Same features and maths as train.forecast, with the model files kept in memory
so a warm Lambda does not reload them on every run."""
import json
from functools import lru_cache
from pathlib import Path

import lightgbm as lgb
import pandas as pd

import train

HISTORY_HOURS = 24 * 15  # the inputs look back at most 7 days; 15 keeps a margin


@lru_cache(maxsize=None)
def _booster(path):
    return lgb.Booster(model_file=path)


@lru_cache(maxsize=None)
def _calibration(path):
    return json.loads(Path(path).read_text())


def clear_cache():
    _booster.cache_clear()
    _calibration.cache_clear()


def serving_frame(df, reading_at, variant, max_h):
    """The slice of history a live system would hold at reading_at: pollutants up to reading_at, and for the
    'fut' set, weather up to the last forecast hour (a real forecast live; archive weather in replay)."""
    reading_at = pd.Timestamp(reading_at)
    start = reading_at - pd.Timedelta(hours=HISTORY_HOURS)
    end = reading_at + pd.Timedelta(hours=max_h if variant == "fut" else 0)
    frame = df.loc[start:end].reindex(pd.date_range(start, end, freq="h"))  # the inputs assume one row per hour
    pollutants = [c for c in ["pm25", "pm10", "no2", "co", "so2"] if c in frame]
    frame.loc[frame.index > reading_at, pollutants] = float("nan")
    return frame


def forecast(df, cfg, model_dir, reading_at, variant="now"):
    """Returns {target time: {'p50', '<high>', 'chance_<label>'...}} for each horizon in cfg."""
    reading_at, model_dir = pd.Timestamp(reading_at), Path(model_dir)
    frame = serving_frame(df, reading_at, variant, max(cfg["horizons"]))
    calib = _calibration(str(model_dir / "calibration.json"))[variant]
    target = cfg.get("target", "level")
    high = [k for k in cfg["quantiles"] if k != "p50"][0]
    ordered = sorted(cfg["labels"], key=cfg["label_thresholds"].get)
    out = {}
    for h in cfg["horizons"]:
        X, _ = train.features(frame, h, train.VARIANTS[variant])
        row = X.loc[[reading_at], cfg["models"][variant]["features"]]
        path = lambda name: str(model_dir / variant / f"{name}_h{h}.txt")
        q = {name: float(train.predict(_booster(path(name)), row, target)[0]) for name in cfg["quantiles"]}
        r = {"p50": round(q["p50"], 1), high: round(max(q[high], q["p50"]), 1)}
        for name in cfg["labels"]:
            raw = _booster(path(f"chance_{name}")).predict(row)
            r[f"chance_{name}"] = float(train.calibrate(raw, calib[name][str(h)])[0])
        for lower, higher in zip(ordered, ordered[1:]):
            r[f"chance_{higher}"] = min(r[f"chance_{higher}"], r[f"chance_{lower}"])
        out[reading_at + pd.Timedelta(hours=h)] = {k: round(v, 3) if k.startswith("chance") else v for k, v in r.items()}
    return out, high
