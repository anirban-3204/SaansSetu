"""Shared test setup.

Tests that need station data and trained models skip when those are absent. By default they look for
./saanssetu_data, the newest ./out*/ folder and ./towns. Point them elsewhere with:
  SAANSSETU_DATA, SAANSSETU_MODELS, SAANSSETU_TOWNS
"""
import copy
import json
import os
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
DATA = Path(os.environ.get("SAANSSETU_DATA", ROOT / "saanssetu_data"))
TOWNS = Path(os.environ.get("SAANSSETU_TOWNS", ROOT / "towns"))


def models_root(station_ids):
    """The newest results folder holding the most of these stations."""
    if os.environ.get("SAANSSETU_MODELS"):
        roots = [Path(os.environ["SAANSSETU_MODELS"])]
    else:
        roots = sorted(ROOT.glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [ROOT / "out"]
    have = lambda r: sum((r / s / "config.json").exists() for s in station_ids)
    roots = [r for r in roots if have(r)]
    return max(roots, key=have) if roots else None


class World:
    """Every town that has both data and models here, plus a morning on which the routed town sends an alert."""
    def __init__(self):
        import train
        from core import pipeline
        from core.townconfig import load_towns
        towns, problems = load_towns(TOWNS)
        assert not problems, f"broken town files: {problems}"
        self.root = models_root([t["station"]["station_id"] for t in towns.values()])
        ok = lambda t: self.root is not None and (self.root / t["station"]["station_id"] / "models" / "calibration.json").exists() \
            and (DATA / f"aq_{t['station']['station_id']}.csv.gz").exists()
        self.towns = {tid: t for tid, t in towns.items() if ok(t)}
        routed = [t for t in self.towns.values() if t["has_routes"]]
        if not routed:
            pytest.skip("no routed town has both data and models here; run fetch_data.py and train.py first")
        self.hero = routed[0]
        self.others = [t for t in self.towns.values() if not t["has_routes"]]
        sid = self.hero["station"]["station_id"]
        self.cfg = json.loads((self.root / sid / "config.json").read_text())
        self.history = train.load(DATA, sid)
        pm = self.history["pm25"]
        self.alert_day = self.quiet_day = None
        for d in pd.date_range(self.cfg["test"][0], self.cfg["test"][1], freq="D"):
            window = pm.loc[d - pd.Timedelta(hours=170): d + pd.Timedelta(hours=9)]
            if window.notna().mean() < 0.9 or pm.loc[d + pd.Timedelta(hours=4): d + pd.Timedelta(hours=9)].isna().any():
                continue
            run = pipeline.run_once(copy.deepcopy(self.hero), self.history, self.cfg, self.root / sid / "models",
                                    d + pd.Timedelta(hours=6, minutes=30), mode="replay")
            if run["alert"]["decision"] == "send" and self.alert_day is None:
                self.alert_day = str(d.date())
            if run["alert"]["decision"] == "none" and run["alert"]["school_flag"]["outdoor_pt"] == "ok" and self.quiet_day is None:
                self.quiet_day = str(d.date())
            if self.alert_day and self.quiet_day:
                break
        if self.alert_day is None:
            pytest.skip("no morning in the held-out winter sends an alert for the routed town")

    def copy_towns(self):
        return copy.deepcopy(self.towns)


@pytest.fixture(scope="session")
def world():
    return World()


@pytest.fixture(scope="session")
def replayed(world):
    """The alert morning replayed once through the live path for every town: (store, frames)."""
    from core import runner
    return runner.replay_day(world.copy_towns(), DATA, world.root, world.alert_day)
