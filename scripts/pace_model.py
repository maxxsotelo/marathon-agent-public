"""Learned pace bands from the athlete's own heart-rate and speed data (build_plan T12b, P34).

For each heart-rate zone the watch gets a secondary pace band: the pace the athlete actually runs at that HR now,
in typical conditions. It is learned weekly from his steady outdoor runs (model_utils.steady_runs):

    speed ~ 1 + avg_hr + heat_index_c + climb_per_km     weights 0.5 ** (age_days / half_life_days)

so the last three weeks count most and the band follows his fitness. HR stays the primary target (KB Rule 9);
the pace band is secondary and moves at most 5 s/km a week (cap_change), so one odd week can't swing it.
A band is only written for a zone the recent runs actually cover (no extrapolating to threshold from easy runs).

    fit_hr_speed(rows, today, half_life_days=21, min_runs=8) -> model | None
    pace_band(model, hr_low, hr_high, heat_index_c=29.0, climb_per_km=4.0) -> (fast_s_per_km, slow_s_per_km) | None
    cap_change(new, old, max_s=5) -> band
    hr_coverage(rows, today, days=90) -> (lowest, highest) average HR of the recent steady runs, or None
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from model_utils import num, predict, wls  # noqa: E402

FEATURES = ("avg_hr", "heat_index_c", "climb_per_km")
BANDS = {"easy": (162, 174), "marathon": (175, 181), "threshold": (182, 191)}   # Z2 (easy and long), Z3, Z4
DEFAULT_HEAT, DEFAULT_CLIMB = 29.0, 4.0
RECENT_DAYS, MIN_RECENT = 28, 3            # the model needs current data, not only old runs
EXTRAPOLATE_BPM = 5                         # a band may reach this far beyond the HRs actually run
PACE_BANDS_FILE = os.path.join(os.path.dirname(HERE), "data", "pace_bands.json")


def _day(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _features(rows: list[dict]) -> list[str]:
    """avg_hr always; heat and climb only when at least half the rows have them (else the fit would lose
    most rows to one missing sensor)."""
    out = ["avg_hr"]
    for f in FEATURES[1:]:
        if sum(num(r.get(f)) is not None for r in rows) >= 0.5 * len(rows):
            out.append(f)
    return out


def fit_hr_speed(rows: list[dict], today: date, half_life_days: float = 21, min_runs: int = 8) -> dict | None:
    data = [r for r in rows if _day(r.get("date")) and _day(r["date"]) <= today
            and num(r.get("avg_hr")) and num(r.get("speed_mps"))]
    if len(data) < min_runs:
        return None
    if sum((today - _day(r["date"])).days <= RECENT_DAYS for r in data) < MIN_RECENT:
        return None
    feats = _features(data)
    weights = [0.5 ** ((today - _day(r["date"])).days / half_life_days) for r in data]
    model = wls(data, "speed_mps", feats, weights)
    if model is None or model["coef"].get("avg_hr", 0) <= 0:      # faster at a higher HR, or no usable model
        return None
    model.update(kind="hr_speed", ref_date=today.isoformat(), half_life_days=half_life_days)
    return model


def pace_band(model: dict, hr_low: float, hr_high: float, heat_index_c: float = DEFAULT_HEAT,
              climb_per_km: float = DEFAULT_CLIMB) -> tuple[float, float] | None:
    """(fast, slow) seconds per km for HR hr_low..hr_high in these conditions."""
    slow_v = predict(model, avg_hr=hr_low, heat_index_c=heat_index_c, climb_per_km=climb_per_km)
    fast_v = predict(model, avg_hr=hr_high, heat_index_c=heat_index_c, climb_per_km=climb_per_km)
    if slow_v <= 0 or fast_v <= slow_v:
        return None
    return round(1000 / fast_v, 1), round(1000 / slow_v, 1)


def cap_change(new, old, max_s: float = 5):
    """Each end of the band moves at most max_s seconds per km from last week's band."""
    if not old:
        return new
    return tuple(o + max(-max_s, min(max_s, n - o)) for n, o in zip(new, old))


def hr_coverage(rows: list[dict], today: date, days: int = 90):
    hrs = [num(r["avg_hr"]) for r in rows if _day(r.get("date")) and 0 <= (today - _day(r["date"])).days <= days
           and num(r.get("avg_hr"))]
    return (min(hrs), max(hrs)) if hrs else None


def band_supported(hr_low: float, hr_high: float, coverage) -> bool:
    return bool(coverage) and hr_low >= coverage[0] - EXTRAPOLATE_BPM and hr_high <= coverage[1] + EXTRAPOLATE_BPM


def read_bands(path: str | None = None) -> dict:
    try:
        with open(path or PACE_BANDS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def fmt_pace(sec: float) -> str:
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"
