"""
SaansSetu: station PM2.5 forecast, +1 to +4 hours.

Two kinds of model, same inputs:
  level    how high: likely value (p50) and bad-case value (p95), in ug/m3. The script measures and
           prints how often the truth actually stayed under the bad-case line; quote that, not "95%".
  chance   how likely the hour is Moderate (61+), Poor (91+) or Very Poor (121+), as a calibrated
           probability. The label that best splits this town's winter school mornings is suggested.

  pip install lightgbm pandas
  python train.py --station site_XXXX                      one station
  python train.py --station site_A site_B site_C           several towns, one summary at the end
  python train.py --station site_XXXX --final              also fit the deployment models

Station ids are in saanssetu_data/scouting.csv (made by fetch_data.py).
Results go to out/, or to out_v2/, out_v3/, ... when an earlier run exists, so nothing is overwritten.
Output per station in <results folder>/<station_id>/: models/ (backtest models), final/ (with --final),
config.json (feature lists, settings), metrics.json, backtest.csv, feature_importance.csv.

Backtest: train on everything before the test winter, test on the held-out winter
(default Oct 2024 to Feb 2025). The level model ships only if it beats persistence on school mornings.
The chance model is trained on the same rows, calibrated on the winter before the test winter, and
must beat the usual rate for that month and hour.

The level model forecasts the change from the latest reading, not the value itself (--target change).
A tree model cannot predict a value higher than any it was trained on, so forecasting the value directly
fails on mornings worse than the training data. Forecasting the change does not have that ceiling.

Two feature sets are trained, saved and reported:
  fut  weather at the target hour (historical weather standing in for a forecast: an upper bound)
  now  weather at the issue hour only (a lower bound, and the fallback when no forecast is available)
Live skill sits between the two, depending on how good the weather forecast is.

Two alert cases are reported for 7-9 AM targets:
  fresh   latest reading is 6 AM  (horizons +1..+3)
  lagged  latest reading is 5 AM  (horizons +2..+4), for when the CPCB feed runs an hour behind

Timestamps are IST. CPCB's hourly stamp convention (start or end of the averaging hour) is not
documented in the source; check it against the live feed before quoting clock times in an alert.
"""

import argparse
import json
import platform
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

HORIZONS = [1, 2, 3, 4]
QUANTILES = {"p50": 0.5, "p95": 0.95}
HIGH = "p95"   # the bad-case line. A 0.90 model held only ~82% of the time on real data, so it aims higher.
# India AQI bands for PM2.5 (defined on 24-hour means; used here on hourly values as a guide)
LABELS = {"moderate": 61.0, "poor": 91.0, "very_poor": 121.0}
POOR = LABELS["poor"]
VARIANTS = {"fut": True, "now": False}
SCHOOL_HOURS = [7, 8, 9]
WINTER_MONTHS = [10, 11, 12, 1, 2]
ALERT_CASES = {"fresh_6am": 6, "lagged_5am": 5}  # hour of the latest reading available
MIN_TRAIN_HOURS = 2000   # about three months; below this the station is skipped
THIN_TRAIN_HOURS = 8760  # under a year: trained, with a warning that it has not seen every season

LGB_PARAMS = dict(learning_rate=0.03, num_leaves=31, min_data_in_leaf=50, feature_fraction=0.8,
                  bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1, seed=7)
CHANCE_PARAMS = dict(LGB_PARAMS, learning_rate=0.05, objective="binary", metric="binary_logloss")


class NotEnoughData(Exception):
    pass


# ---------------------------------------------------------------- data

def _read(path):
    return pd.read_csv(path, parse_dates=["time"], index_col="time")


def find_weather(d, sid):
    own = d / f"wx_{sid}.csv.gz"
    if own.exists():
        return own
    st = d / "stations.csv"
    if st.exists():
        s = pd.read_csv(st)
        row = s[s["station_id"] == sid]
        if len(row) and isinstance(row.iloc[0].get("wx_file"), str) and (d / row.iloc[0]["wx_file"]).exists():
            return d / row.iloc[0]["wx_file"]
    return None


def station_label(d, sid):
    st = d / "stations.csv"
    if st.exists():
        s = pd.read_csv(st)
        row = s[s["station_id"] == sid]
        if len(row):
            return str(row.iloc[0].get("station_name", sid))
    return sid


def load(data_dir, sid):
    d = Path(data_dir)
    aq_path = d / f"aq_{sid}.csv.gz"
    if not aq_path.exists():
        raise NotEnoughData(f"{aq_path} not found")
    aq = _read(aq_path)
    if "pm25" not in aq or aq["pm25"].notna().sum() == 0:
        raise NotEnoughData("station has no PM2.5")
    idx = pd.date_range(aq.index.min().floor("h"), aq.index.max().floor("h"), freq="h")
    aq = clean(aq.groupby(aq.index.floor("h")).mean().reindex(idx))
    wx_path = find_weather(d, sid)
    if wx_path is None:
        print("  warning: no weather file found, training on pollutant history only")
        return aq
    wx = _read(wx_path)
    return aq.join(wx[~wx.index.duplicated()].reindex(idx))


def clean(aq):
    aq = aq.copy()
    for c in aq.columns:
        s = aq[c]
        s = s.where((s > 0) & (s < 1500))
        # stuck sensor: the same value for 6+ hours in a row
        run = s.groupby((s != s.shift()).cumsum()).transform("size")
        aq[c] = s.where(~((run >= 6) & s.notna()))
    return aq


def weather_cols(df, suffix, shift):
    out = {}
    if "wind_speed_10m" in df and "wind_direction_10m" in df:
        ws = df["wind_speed_10m"].shift(shift)
        wd = np.deg2rad(df["wind_direction_10m"].shift(shift))
        out[f"ws_{suffix}"] = ws
        out[f"u_{suffix}"] = -ws * np.sin(wd)
        out[f"v_{suffix}"] = -ws * np.cos(wd)
    for src, name in [("boundary_layer_height", "blh"), ("temperature_2m", "t2m"),
                      ("relative_humidity_2m", "rh"), ("precipitation", "rain"),
                      ("cloud_cover", "cloud"), ("surface_pressure", "psfc")]:
        if src in df:
            out[f"{name}_{suffix}"] = df[src].shift(shift)
    if "temperature_2m" in df and "dew_point_2m" in df:
        out[f"dewdep_{suffix}"] = (df["temperature_2m"] - df["dew_point_2m"]).shift(shift)
    if "boundary_layer_height" in df and "wind_speed_10m" in df:
        # ventilation index: how much air is available to dilute what is emitted
        out[f"vent_{suffix}"] = (df["boundary_layer_height"] * df["wind_speed_10m"]).shift(shift)
    return out


def features(df, h, future_wx):
    """Features known at issue time t (plus, if future_wx, forecastable weather at t+h). Target: PM2.5 at t+h."""
    pm = df["pm25"]
    X = {}
    for lag in [0, 1, 2, 3, 6, 12, 24]:
        X[f"pm25_lag{lag}"] = pm.shift(lag)
    X["pm25_same_hour_yday"] = pm.shift(24 - h)
    X["pm25_same_hour_lastwk"] = pm.shift(168 - h)
    X["pm25_mean3"] = pm.rolling(3, min_periods=2).mean()
    X["pm25_mean6"] = pm.rolling(6, min_periods=3).mean()
    X["pm25_mean24"] = pm.rolling(24, min_periods=12).mean()
    X["pm25_slope3"] = pm - pm.shift(3)
    for c in ["pm10", "no2", "co", "so2"]:
        if c in df:
            X[f"{c}_now"] = df[c]
    if "pm10" in df:
        X["pm_ratio"] = pm / df["pm10"]
    tt = df.index + pd.Timedelta(hours=h)
    X["target_hour"] = pd.Series(tt.hour, index=df.index)
    X["target_dow"] = pd.Series(tt.dayofweek, index=df.index)
    X["doy_sin"] = pd.Series(np.sin(2 * np.pi * tt.dayofyear / 365.25), index=df.index)
    X["doy_cos"] = pd.Series(np.cos(2 * np.pi * tt.dayofyear / 365.25), index=df.index)
    X.update(weather_cols(df, "now", 0))
    if future_wx:
        fut = weather_cols(df, "fut", -h)
        X.update(fut)
        if "blh_fut" in fut:
            X["blh_change"] = fut["blh_fut"] - X["blh_now"]
    X = pd.DataFrame(X, index=df.index)
    y = pm.shift(-h)
    return X, y


def split(t, h, test_start, test_end):
    """Train / validation / test masks over issue times t. Validation is the winter before the test winter."""
    dh = pd.Timedelta(hours=h)
    val_start, val_end = test_start - pd.DateOffset(years=1), test_end - pd.DateOffset(years=1)
    tr = (t + dh < val_start) | ((t > val_end) & (t + dh < test_start))
    va = (t >= val_start) & (t <= val_end)
    te = (t >= test_start) & (t <= test_end)
    if te.sum() < 200:
        raise NotEnoughData(f"only {te.sum()} usable hours in the test winter")
    if va.sum() < 500:  # validation winter missing: use the last 15% of training rows instead
        if tr.sum() < MIN_TRAIN_HOURS:
            raise NotEnoughData(f"only {tr.sum()} usable training hours before {test_start.date()} (needs {MIN_TRAIN_HOURS})")
        cut = t[tr][int(tr.sum() * 0.85)]
        va, tr = tr & (t >= cut), tr & (t < cut)
    if tr.sum() < MIN_TRAIN_HOURS:
        raise NotEnoughData(f"only {tr.sum()} usable training hours before {test_start.date()} (needs {MIN_TRAIN_HOURS})")
    return tr, va, te


# ---------------------------------------------------------------- baselines

def month_hour_table(series, how):
    return getattr(series.groupby([series.index.month, series.index.hour]), how)()


def lookup(table, times, default):
    return np.nan_to_num(table.reindex(list(zip(times.month, times.hour))).to_numpy(dtype=float), nan=default)


def diurnal_baseline(pm, h, table):
    """Last reading scaled by the usual month-and-hour pattern (learned on training data only)."""
    t = pm.index
    tt = t + pd.Timedelta(hours=h)
    ratio = np.clip(lookup(table, tt, np.nan) / lookup(table, t, np.nan), 0.3, 3.0)
    return pm.to_numpy() * np.nan_to_num(ratio, nan=1.0)


# ---------------------------------------------------------------- level models (quantiles)

def to_target(y, X, target):
    """'change': the model learns how PM2.5 will move from the latest reading (log ratio), so a morning
    worse than anything in the training data is still forecast from where it actually is.
    'level': the model learns the value itself, and can never predict above what it has seen."""
    z = np.log1p(y)
    return z - np.log1p(X["pm25_lag0"]) if target == "change" else z


def predict(m, X, target):
    z = m.predict(X)
    if target == "change":
        z = z + np.log1p(X["pm25_lag0"].to_numpy())
    return np.maximum(np.expm1(z), 0.0)


def fit_quantile(Xtr, ytr, Xva, yva, alpha, target):
    p = dict(LGB_PARAMS, objective="quantile", alpha=alpha, metric="quantile")
    dtr = lgb.Dataset(Xtr, to_target(ytr, Xtr, target))
    dva = lgb.Dataset(Xva, to_target(yva, Xva, target), reference=dtr)
    m = lgb.train(p, dtr, num_boost_round=4000, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(150, verbose=False)])
    best = max(m.best_iteration, 50)
    X_all, y_all = pd.concat([Xtr, Xva]), pd.concat([ytr, yva])
    final = lgb.train(p, lgb.Dataset(X_all, to_target(y_all, X_all, target)), num_boost_round=int(best * 1.1))
    return final, best


# ---------------------------------------------------------------- chance models (calibrated probability)

def isotonic_knots(p, y, bins=30):
    """Monotone map from raw score to observed frequency, as knots for np.interp.
    Scores are grouped into equal-count bins, then pool-adjacent-violators makes the map non-decreasing."""
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        return {"x": [0.0, 1.0], "y": [float(np.clip(y.mean(), 0.01, 0.99))] * 2}
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    w = np.bincount(idx, minlength=len(edges) - 1).astype(float)
    keep = w > 0
    x = np.bincount(idx, weights=p, minlength=len(edges) - 1)[keep] / w[keep]
    v = np.bincount(idx, weights=y, minlength=len(edges) - 1)[keep] / w[keep]
    blocks = []  # [value, weight, n_points]
    for vi, wi in zip(v, w[keep]):
        blocks.append([vi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            b = blocks[-1]
            b[0], b[1], b[2] = (b[0] * b[1] + v2 * w2) / (b[1] + w2), b[1] + w2, b[2] + n2
    fitted = np.concatenate([[b[0]] * b[2] for b in blocks])
    return {"x": [round(float(a), 5) for a in x], "y": [round(float(a), 4) for a in np.clip(fitted, 0.01, 0.99)]}


def calibrate(raw, knots):
    return np.interp(raw, knots["x"], knots["y"])


def fit_chance(Xtr, ytr, Xva, yva):
    """Classifier trained on the training rows; calibration map learned on the validation winter,
    which the classifier never trained on."""
    dtr = lgb.Dataset(Xtr, ytr)
    m = lgb.train(CHANCE_PARAMS, dtr, num_boost_round=3000, valid_sets=[lgb.Dataset(Xva, yva, reference=dtr)],
                  callbacks=[lgb.early_stopping(100, verbose=False)])
    best = max(m.best_iteration, 30)
    return m, best, isotonic_knots(m.predict(Xva, num_iteration=best), yva)


def usable_labels(pm, test_start, test_end):
    """Labels with enough hours on both sides of the threshold to train and to calibrate, plus the
    one that best splits winter school mornings (closest to half)."""
    val_start, val_end = test_start - pd.DateOffset(years=1), test_end - pd.DateOffset(years=1)
    pm = pm.dropna()
    train = pm[(pm.index < val_start) | ((pm.index > val_end) & (pm.index < test_start))]
    val = pm[(pm.index >= val_start) & (pm.index <= val_end)]
    if len(val) < 500:
        val = train.iloc[int(len(train) * 0.85):]
    before = pm[pm.index < test_start]
    school = before[before.index.month.isin(WINTER_MONTHS) & before.index.hour.isin(SCHOOL_HOURS)]
    labels, rates = [], {}
    for name, thr in LABELS.items():
        enough = min((train >= thr).sum(), (train < thr).sum()) >= 300 and min((val >= thr).sum(), (val < thr).sum()) >= 50
        if enough:
            labels.append(name)
            rates[name] = round(float((school >= thr).mean()), 3) if len(school) else None
        else:
            print(f"  chance model for '{name}' ({thr:.0f}+) skipped: too few hours on one side of the threshold")
    scored = [n for n in labels if rates[n] is not None]
    suggested = min(scored, key=lambda n: abs(rates[n] - 0.5)) if scored else None
    return labels, rates, suggested


# ---------------------------------------------------------------- scoring

def mae(a, b):
    return float(np.mean(np.abs(np.asarray(a) - np.asarray(b))))


def detection(actual, pred_flag, thr=POOR):
    actual = np.asarray(actual) >= thr
    pred_flag = np.asarray(pred_flag)
    hits = int((actual & pred_flag).sum())
    misses = int((actual & ~pred_flag).sum())
    false = int((~actual & pred_flag).sum())
    return {"events": int(actual.sum()), "hit_rate": round(hits / max(hits + misses, 1), 3),
            "false_alarm_ratio": round(false / max(hits + false, 1), 3),
            "csi": round(hits / max(hits + misses + false, 1), 3)}


def bootstrap_skill(issue_time, err_model, err_ref, n=1000, seed=7):
    """Day-block bootstrap of 1 - sum(err_model) / sum(err_ref). Returns a 95% interval."""
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(pd.DatetimeIndex(issue_time).normalize(), return_inverse=True)
    sm = np.bincount(inv, weights=np.asarray(err_model, dtype=float), minlength=len(uniq))
    sr = np.bincount(inv, weights=np.asarray(err_ref, dtype=float), minlength=len(uniq))
    out = []
    for _ in range(n):
        pick = rng.integers(0, len(uniq), len(uniq))
        ref = sr[pick].sum()
        if ref > 0:
            out.append(1 - sm[pick].sum() / ref)
    if not out:
        return [None, None]
    return [round(float(np.percentile(out, 2.5)), 3), round(float(np.percentile(out, 97.5)), 3)]


def auc(y, p):
    y = np.asarray(y).astype(bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return None
    r = pd.Series(np.asarray(p)).rank().to_numpy()
    return round(float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)), 3)


def score_level(s):
    a = s["actual"].to_numpy()
    m_p50, m_per, m_diu = mae(s["p50"], a), mae(s["persistence"], a), mae(s["diurnal"], a)
    return {
        "n": int(len(s)),
        "mae_model": round(m_p50, 2), "mae_persistence": round(m_per, 2), "mae_diurnal": round(m_diu, 2),
        "skill_vs_persistence": round(1 - m_p50 / m_per, 3),
        "skill_vs_diurnal": round(1 - m_p50 / m_diu, 3),
        f"{HIGH}_coverage": round(float((a <= s[HIGH]).mean()), 3),
        "poor_detect_model_p50": detection(a, s["p50"] >= POOR),
        f"poor_detect_model_{HIGH}": detection(a, s[HIGH] >= POOR),
        "poor_detect_persistence": detection(a, s["persistence"] >= POOR),
    }


def score_chance(s, label):
    """Brier score: mean squared gap between the stated chance and what happened (0 or 1). Lower is better."""
    thr = LABELS[label]
    o = (s["actual"].to_numpy() >= thr).astype(float)
    p, usual = s[f"chance_{label}"].to_numpy(), s[f"usual_{label}"].to_numpy()
    pers = (s["persistence"].to_numpy() >= thr).astype(float)
    b_model, b_usual, b_pers = float(np.mean((p - o) ** 2)), float(np.mean((usual - o) ** 2)), float(np.mean((pers - o) ** 2))
    edges10 = np.linspace(0, 1, 11)
    k = np.clip(np.digitize(p, edges10) - 1, 0, 9)
    ece = sum(abs(p[k == i].mean() - o[k == i].mean()) * (k == i).mean() for i in range(10) if (k == i).any())
    rel = []
    for lo in [0.0, 0.2, 0.4, 0.6, 0.8]:
        m = (p >= lo) & (p < lo + 0.2 if lo < 0.8 else p <= 1.0)
        if m.any():
            rel.append({"said": f"{int(lo * 100)}-{int(lo * 100) + 20}%", "n": int(m.sum()),
                        "mean_said": round(float(p[m].mean()), 3), "happened": round(float(o[m].mean()), 3)})
    return {
        "n": int(len(s)), "event_rate": round(float(o.mean()), 3),
        "brier_model": round(b_model, 4), "brier_usual_rate": round(b_usual, 4), "brier_persistence": round(b_pers, 4),
        "skill_vs_usual_rate": round(1 - b_model / b_usual, 3) if b_usual > 0 else None,
        "skill_vs_usual_rate_ci95": bootstrap_skill(s["issue_time"], (p - o) ** 2, (usual - o) ** 2),
        "skill_vs_persistence": round(1 - b_model / b_pers, 3) if b_pers > 0 else None,
        "auc": auc(o, p), "calibration_error": round(float(ece), 3),
        "at_50pct": detection(s["actual"], p >= 0.5, thr), "reliability": rel,
    }


# ---------------------------------------------------------------- backtest

def run(df, args, out):
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end) + pd.Timedelta(hours=23)
    pm = df["pm25"]
    before = pm[pm.index < test_start].dropna()
    med_table = month_hour_table(before, "median")
    labels, rates, suggested = usable_labels(pm, test_start, test_end)
    rate_tables = {n: month_hour_table((before >= LABELS[n]).astype(float), "mean") for n in labels}
    overall = {n: float((before >= LABELS[n]).mean()) for n in labels}

    results, rows, importances = {v: {} for v in VARIANTS}, [], {}
    meta = {v: {} for v in VARIANTS}
    calib = {v: {n: {} for n in labels} for v in VARIANTS}
    train_hours = None
    for variant, fut in VARIANTS.items():
        mdir = out / "models" / variant
        mdir.mkdir(parents=True, exist_ok=True)
        for h in HORIZONS:
            X, y = features(df, h, fut)
            ok = y.notna() & X["pm25_lag0"].notna()
            X, y = X[ok], y[ok]
            t = X.index
            tr, va, te = split(t, h, test_start, test_end)
            if train_hours is None:
                train_hours = int(tr.sum() + va.sum())
            target_time = t[te] + pd.Timedelta(hours=h)

            preds = {}
            for qname, alpha in QUANTILES.items():
                m, best = fit_quantile(X[tr], y[tr], X[va], y[va], alpha, args.target)
                preds[qname] = predict(m, X[te], args.target)
                path = mdir / f"{qname}_h{h}.txt"
                m.save_model(str(path))
                # the saved file must reproduce the in-memory model
                again = predict(lgb.Booster(model_file=str(path)), X[te].head(200), args.target)
                assert np.allclose(again, preds[qname][:200], rtol=1e-6), f"saved model mismatch: {path}"
                meta[variant][f"{qname}_h{h}"] = {"best_iter": best}
                if variant == "fut" and qname == "p50":
                    importances[f"h{h}"] = pd.Series(m.feature_importance("gain"), index=X.columns)
            meta[variant]["features"] = list(X.columns)

            bt = pd.DataFrame({
                "variant": variant, "h": h, "issue_time": t[te], "target_time": target_time,
                "actual": y[te].to_numpy(), "p50": preds["p50"],
                HIGH: np.maximum(preds[HIGH], preds["p50"]),  # the two models are fit separately
                "persistence": X.loc[te, "pm25_lag0"].to_numpy(),
                "diurnal": diurnal_baseline(X.loc[te, "pm25_lag0"], h, med_table),
            })
            for name in labels:
                yb = (y >= LABELS[name]).astype(int)
                m, best, knots = fit_chance(X[tr], yb[tr], X[va], yb[va])
                path = mdir / f"chance_{name}_h{h}.txt"
                m.save_model(str(path), num_iteration=best)
                raw = m.predict(X[te], num_iteration=best)
                assert np.allclose(lgb.Booster(model_file=str(path)).predict(X[te].head(200)), raw[:200], rtol=1e-6)
                calib[variant][name][str(h)] = knots
                meta[variant][f"chance_{name}_h{h}"] = {"best_iter": best}
                bt[f"chance_{name}"] = calibrate(raw, knots)
                bt[f"usual_{name}"] = lookup(rate_tables[name], target_time, overall[name])
            ordered = sorted(labels, key=LABELS.get)
            for lower, higher in zip(ordered, ordered[1:]):  # a higher bar cannot be more likely
                bt[f"chance_{higher}"] = np.minimum(bt[f"chance_{higher}"], bt[f"chance_{lower}"])
            rows.append(bt)
            school = bt[bt["target_time"].dt.hour.isin(SCHOOL_HOURS)]
            results[variant][f"h{h}"] = {"all_hours": score_level(bt),
                                         "school_7to9": score_level(school) if len(school) else None}

    (out / "models" / "calibration.json").write_text(json.dumps(calib))
    bt_all = pd.concat(rows, ignore_index=True)
    num = [c for c in bt_all.columns if c not in ("variant", "h", "issue_time", "target_time")]
    bt_all[num] = bt_all[num].round(3)
    bt_all.to_csv(out / "backtest.csv", index=False)
    for variant in VARIANTS:
        for case, hour in ALERT_CASES.items():
            a = bt_all[(bt_all["variant"] == variant) & (bt_all["issue_time"].dt.hour == hour)
                       & bt_all["target_time"].dt.hour.isin(SCHOOL_HOURS)]
            if len(a) < 30:
                results[variant][case] = None
                continue
            err = lambda col: np.abs(a[col].to_numpy() - a["actual"].to_numpy())
            results[variant][case] = score_level(a) | {
                "skill_vs_persistence_ci95": bootstrap_skill(a["issue_time"], err("p50"), err("persistence")),
                "skill_vs_diurnal_ci95": bootstrap_skill(a["issue_time"], err("p50"), err("diurnal")),
                "chance": {n: score_chance(a, n) for n in labels},
            }
    imp = pd.DataFrame(importances)
    imp.to_csv(out / "feature_importance.csv")
    info = {"labels": labels, "winter_school_morning_rate": rates, "suggested_label": suggested, "train_hours": train_hours}
    return results, meta, imp, info


def final_fit(df, meta, labels, args, out):
    """Deployment models. Level models use all data. Chance models train on everything except the
    latest winter and calibrate on it, which is exactly the procedure the backtest scored."""
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end) + pd.Timedelta(hours=23)
    calib = {v: {n: {} for n in labels} for v in VARIANTS}
    for variant, fut in VARIANTS.items():
        fdir = out / "final" / variant
        fdir.mkdir(parents=True, exist_ok=True)
        for h in HORIZONS:
            X, y = features(df, h, fut)
            ok = y.notna() & X["pm25_lag0"].notna()
            X, y = X[ok], y[ok]
            for qname, alpha in QUANTILES.items():
                n = int(meta[variant][f"{qname}_h{h}"]["best_iter"] * 1.1)
                p = dict(LGB_PARAMS, objective="quantile", alpha=alpha)
                lgb.train(p, lgb.Dataset(X, to_target(y, X, args.target)), num_boost_round=n).save_model(str(fdir / f"{qname}_h{h}.txt"))
            t = X.index
            cal = (t >= test_start) & (t <= test_end)
            trn = (t + pd.Timedelta(hours=h) < test_start) | (t > test_end)
            for name in labels:
                yb = (y >= LABELS[name]).astype(int)
                n = meta[variant][f"chance_{name}_h{h}"]["best_iter"]
                m = lgb.train(CHANCE_PARAMS, lgb.Dataset(X[trn], yb[trn]), num_boost_round=n)
                m.save_model(str(fdir / f"chance_{name}_h{h}.txt"))
                calib[variant][name][str(h)] = isotonic_knots(m.predict(X[cal]), yb[cal])
    (out / "final" / "calibration.json").write_text(json.dumps(calib))


def forecast(df, cfg, model_dir, t, variant="fut"):
    """Forecast from the reading stamped t. df: hourly frame as returned by load(), holding pollutants up
    to t and, for the 'fut' set, forecast weather up to t + max horizon. model_dir is .../models or
    .../final. Use this same function when serving so the features match training exactly."""
    t, model_dir = pd.Timestamp(t), Path(model_dir)
    calib = json.loads((model_dir / "calibration.json").read_text())[variant]
    out = {}
    for h in cfg["horizons"]:
        X, _ = features(df, h, VARIANTS[variant])
        row = X.loc[[t], cfg["models"][variant]["features"]]
        booster = lambda name: lgb.Booster(model_file=str(model_dir / variant / f"{name}_h{h}.txt"))
        q = {name: float(predict(booster(name), row, cfg.get("target", "level"))[0]) for name in cfg["quantiles"]}
        high = [k for k in cfg["quantiles"] if k != "p50"][0]
        r = {"p50": round(q["p50"], 1), high: round(max(q[high], q["p50"]), 1)}
        for name in cfg["labels"]:
            r[f"chance_{name}"] = float(calibrate(booster(f"chance_{name}").predict(row), calib[name][str(h)])[0])
        ordered = sorted(cfg["labels"], key=cfg["label_thresholds"].get)
        for lower, higher in zip(ordered, ordered[1:]):
            r[f"chance_{higher}"] = min(r[f"chance_{higher}"], r[f"chance_{lower}"])
        out[str(t + pd.Timedelta(hours=h))] = {k: round(v, 3) if k.startswith("chance") else v for k, v in r.items()}
    return out


# ---------------------------------------------------------------- reporting

def report(results, imp, info):
    print("\n  LEVEL MODEL. School mornings (targets 7-9 AM), held-out winter. MAE in ug/m3. skill = 1 - MAE / baseline MAE")
    print(f"  {'set':4} {'h':>2} {'n':>5} {'model':>7} {'persist':>8} {'diurnal':>8} {'skill_p':>8} {'skill_d':>8} {HIGH + 'cov':>7}")
    for v in VARIANTS:
        for h in HORIZONS:
            s = results[v][f"h{h}"]["school_7to9"]
            if s:
                print(f"  {v:4} {h:>2} {s['n']:>5} {s['mae_model']:>7} {s['mae_persistence']:>8} {s['mae_diurnal']:>8} "
                      f"{s['skill_vs_persistence']:>8} {s['skill_vs_diurnal']:>8} {s[HIGH + '_coverage']:>7}")
    for case in ALERT_CASES:
        for v in VARIANTS:
            a = results[v][case]
            if not a:
                print(f"\n  {case} ({v}): too few mornings to score")
                continue
            d, dp = a["poor_detect_model_p50"], a["poor_detect_persistence"]
            print(f"\n  {case} ({v}): skill vs persistence {a['skill_vs_persistence']} {a['skill_vs_persistence_ci95']}, "
                  f"vs diurnal {a['skill_vs_diurnal']} {a['skill_vs_diurnal_ci95']}")
            print(f"    Poor hours: {d['events']} of {a['n']}. Model: hit rate {d['hit_rate']}, false alarms {d['false_alarm_ratio']}. "
                  f"Persistence: hit rate {dp['hit_rate']}, false alarms {dp['false_alarm_ratio']}")
            print(f"    Bad-case line ({HIGH} model): the real value stayed under it {a[HIGH + '_coverage']:.0%} of the time")
    print("\n  Top features (p50, +2h, by gain):")
    print("  " + imp["h2"].sort_values(ascending=False).head(8).round(0).to_string().replace("\n", "\n  "))

    if not info["labels"]:
        print("\n  CHANCE MODEL: not trained (no usable label).")
        return
    print("\n  CHANCE MODEL. 7-9 AM targets, held-out winter. Brier score: lower is better.")
    print("  'usual' = the usual rate for that month and hour. skill = 1 - Brier / usual Brier")
    print(f"  {'case':11} {'set':4} {'label':10} {'event%':>7} {'model':>7} {'usual':>7} {'persist':>8} {'skill':>6} {'95% CI':>16} {'AUC':>6} {'cal.err':>8}")
    for case in ALERT_CASES:
        for v in VARIANTS:
            a = results[v][case]
            for name in info["labels"] if a else []:
                c = a["chance"][name]
                print(f"  {case:11} {v:4} {name:10} {c['event_rate'] * 100:>6.1f}% {c['brier_model']:>7} {c['brier_usual_rate']:>7} "
                      f"{c['brier_persistence']:>8} {str(c['skill_vs_usual_rate']):>6} {str(c['skill_vs_usual_rate_ci95']):>16} "
                      f"{str(c['auc']):>6} {c['calibration_error']:>8}")
    s = info["suggested_label"]
    if s:
        rate = info["winter_school_morning_rate"][s]
        print(f"\n  Suggested label: {s} ({LABELS[s]:.0f}+ ug/m3), true on {rate:.0%} of winter school-morning hours before the test winter")
        a = results["fut"]["fresh_6am"]
        if a:
            print(f"  When the model said ... it happened ... (fresh_6am, fut, {s}):")
            for r in a["chance"][s]["reliability"]:
                print(f"    {r['said']:>8}  ->  {r['happened'] * 100:5.1f}%   ({r['n']} hours, average stated {r['mean_said'] * 100:.0f}%)")


def verdict(results):
    a = results["now"]["fresh_6am"]
    if not a:
        return "NO VERDICT", None
    lo = a["skill_vs_persistence_ci95"][0]
    return ("SHIP" if lo is not None and lo > 0 else "DO NOT SHIP"), lo


def chance_verdict(results, info):
    a, s = results["now"]["fresh_6am"], info["suggested_label"]
    if not a or not s:
        return "NO VERDICT", None
    skill, lo = a["chance"][s]["skill_vs_usual_rate"], a["chance"][s]["skill_vs_usual_rate_ci95"][0]
    if skill is None or lo is None or skill <= 0:
        return "DO NOT USE", lo
    return ("USE" if lo > 0 else "UNPROVEN"), lo


def next_out_folder():
    """out if it is free, otherwise out_v2, out_v3, ... so an earlier run is never overwritten."""
    if not Path("out").exists():
        return "out"
    n = 2
    while Path(f"out_v{n}").exists():
        n += 1
    return f"out_v{n}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="saanssetu_data")
    ap.add_argument("--station", required=True, nargs="+")
    ap.add_argument("--test-start", default="2024-10-01")
    ap.add_argument("--test-end", default="2025-02-28")
    ap.add_argument("--out", default=None, help="results folder. Default: out, or out_v2, out_v3, ... if that already exists")
    ap.add_argument("--final", action="store_true", help="also fit the deployment models")
    ap.add_argument("--target", choices=["change", "level"], default="change",
                    help="change (default): forecast the move from the latest reading. level: forecast the value itself")
    args = ap.parse_args()
    if args.out is None:
        args.out = next_out_folder()
    print(f"Results will be saved in {args.out}/")

    summary = []
    for sid in args.station:
        name = station_label(Path(args.data), sid)
        print(f"\n=== {name} ({sid}) ===")
        out = Path(args.out) / sid
        try:
            df = load(args.data, sid)
            print(f"  {df.index.min()} to {df.index.max()}, PM2.5 present {df['pm25'].notna().mean():.0%} of hours")
            out.mkdir(parents=True, exist_ok=True)
            results, meta, imp, info = run(df, args, out)
        except NotEnoughData as e:
            print(f"  skipped: {e}")
            summary.append({"station": name, "station_id": sid, "level": f"SKIPPED ({e})"})
            continue

        v, lo = verdict(results)
        cv, clo = chance_verdict(results, info)
        cfg = {"station_id": sid, "station": name, "test": [args.test_start, args.test_end],
               "horizons": HORIZONS, "quantiles": QUANTILES, "school_hours": SCHOOL_HOURS,
               "alert_cases": ALERT_CASES, "target": args.target, "bad_case": HIGH,
               "predict_level": ("expm1(booster.predict(X[features]) + log1p(X.pm25_lag0))" if args.target == "change"
                                 else "expm1(booster.predict(X[features]))") + f"; then {HIGH} = max({HIGH}, p50)",
               "predict_chance": "np.interp(booster.predict(X[features]), knots.x, knots.y) with knots from calibration.json",
               "labels": info["labels"], "label_thresholds": LABELS, "suggested_label": info["suggested_label"],
               "winter_school_morning_rate": info["winter_school_morning_rate"],
               "train_hours": info["train_hours"], "models": meta, "verdict_level": v, "verdict_chance": cv,
               "lightgbm": lgb.__version__, "python": platform.python_version()}
        (out / "config.json").write_text(json.dumps(cfg, indent=2))
        (out / "metrics.json").write_text(json.dumps(results, indent=2))
        report(results, imp, info)
        print(f"\n  Level model: {v} (rule: 6 AM alert beats persistence even without a weather forecast; lower 95% bound {lo})")
        print(f"  Chance model: {cv} (rule: beats the usual rate for that month and hour, same case; lower 95% bound {clo}."
              f" UNPROVEN = better on this winter, but not by enough to rule out luck)")
        if info["train_hours"] < THIN_TRAIN_HOURS:
            print(f"  Caution: only {info['train_hours']:,} hours of history before the test winter (under a year). "
                  f"The model has not seen every season, so treat these scores as provisional.")
        if args.final:
            final_fit(df, meta, info["labels"], args, out)
            print(f"  Final models saved to {out / 'final'}")
        print(f"  Saved: {out}")

        row = {"station": name, "station_id": sid, "train_hours": info["train_hours"], "level": v}
        for case in ALERT_CASES:
            for var in VARIANTS:
                a = results[var][case]
                row[f"{case}_{var}_skill"] = a["skill_vs_persistence"] if a else None
        a = results["fut"]["fresh_6am"]
        row["mae_model"] = a["mae_model"] if a else None
        row["mae_persistence"] = a["mae_persistence"] if a else None
        row["bad_case_held"] = a[HIGH + "_coverage"] if a else None
        s = info["suggested_label"]
        row["chance"], row["chance_label"] = cv, s
        row["chance_skill"] = a["chance"][s]["skill_vs_usual_rate"] if a and s else None
        row["chance_event_rate"] = a["chance"][s]["event_rate"] if a and s else None
        summary.append(row)

    if len(summary) > 1:
        s = pd.DataFrame(summary)
        Path(args.out).mkdir(parents=True, exist_ok=True)
        s.to_csv(Path(args.out) / "summary.csv", index=False)
        pd.set_option("display.width", 250, "display.max_columns", 30, "display.max_colwidth", 40)
        print("\n=== Summary (6 AM alert, 7-9 AM targets; level skill vs persistence, chance skill vs usual rate) ===")
        print(s.to_string(index=False))


if __name__ == "__main__":
    main()
