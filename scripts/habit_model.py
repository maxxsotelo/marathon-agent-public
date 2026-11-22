"""Habit model: how the athlete's planned days actually go (build_plan T19, ROADMAP C2).

Learned from data/ledger/plan.csv (plan vs Garmin, one row per day): which planned runs get done on which
weekday, how much runs end up longer or shorter than planned, and how often a rest day gets a run anyway.

It works *with* his habits without feeding the ones the deep review flagged (P41, reviews/2026-11-deep/):
  - a slot that keeps getting skipped is a plan problem: move the session or make the day a rest day;
  - an overrun is never "fixed" by planning more: the plan stays the ceiling, with the bonus cap written down;
  - runs on rest days are counted as over-plan, and the week keeps one fully off day.

    completion_rates(rows) -> {(weekday, planned_type): rate}
    overrun_ratio(rows, planned_type) -> median actual / planned km of the done runs (None without data)
    extra_runs(rows) -> (rest days with a run, rest days)
    planning_advice(rows) -> list[str]
    skip_probability(rows, weekday, planned_type, planned_km) -> float | None   (numpy logistic, 8+ weeks of rows)
The model describes and suggests; it never changes a gate or a plan by itself.
"""

from __future__ import annotations

import os
import statistics as st
import sys
from datetime import date

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from model_utils import num  # noqa: E402

RUN_TYPES = ("easy", "long_run", "tempo", "intervals", "recovery", "run")
REST_TYPES = ("rest", "gym")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
NAMES = {"easy": "easy runs", "long_run": "long runs", "tempo": "tempo runs", "intervals": "interval sessions",
         "recovery": "recovery runs", "run": "runs"}
DAY_NAMES = dict(zip(WEEKDAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))
MIN_N = 3                 # a pattern needs at least this many planned days
LOW_RATE = 0.5            # done this often or less: the slot is a problem
OVERRUN, UNDERRUN = 1.10, 0.90


def done_value(v):
    """True / False / None (unknown) from plan.csv's done column (1/0/'' or booleans)."""
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return None
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return str(v).strip().lower() in ("true", "yes")


def _runs(rows):
    return [r for r in rows if r.get("planned_type") in RUN_TYPES and done_value(r.get("done")) is not None]


def completion_rates(rows: list[dict]) -> dict[tuple[str, str], float]:
    groups: dict[tuple[str, str], list[bool]] = {}
    for r in _runs(rows):
        groups.setdefault((r["weekday"], r["planned_type"]), []).append(done_value(r["done"]))
    return {k: sum(v) / len(v) for k, v in groups.items()}


def counts(rows: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    groups: dict[tuple[str, str], list[bool]] = {}
    for r in _runs(rows):
        groups.setdefault((r["weekday"], r["planned_type"]), []).append(done_value(r["done"]))
    return {k: (sum(v), len(v)) for k, v in groups.items()}


def ratios(rows: list[dict], planned_type: str) -> list[float]:
    out = []
    for r in _runs(rows):
        p, a = num(r.get("planned_km")), num(r.get("actual_km"))
        if r["planned_type"] == planned_type and done_value(r["done"]) and p and a:
            out.append(a / p)
    return out


def overrun_ratio(rows: list[dict], planned_type: str) -> float | None:
    vals = ratios(rows, planned_type)
    return st.median(vals) if vals else None


def extra_runs(rows: list[dict]) -> tuple[int, int]:
    rest = [r for r in rows if r.get("planned_type") in REST_TYPES and num(r.get("actual_km")) is not None]
    return sum(num(r["actual_km"]) > 0 for r in rest), len(rest)


def planning_advice(rows: list[dict]) -> list[str]:
    advice = []
    for (wd, kind), (done, n) in sorted(counts(rows).items(), key=lambda kv: WEEKDAYS.index(kv[0][0])
                                        if kv[0][0] in WEEKDAYS else 9):
        if n >= MIN_N and done / n <= LOW_RATE:
            advice.append(f"{DAY_NAMES.get(wd, wd)} ({wd}) {NAMES.get(kind, kind)} done {done} of {n}: move that session "
                          f"or make {wd} a rest day. A slot that keeps getting skipped is a plan problem.")
    for kind in RUN_TYPES:
        vals = ratios(rows, kind)
        if len(vals) < MIN_N:
            continue
        med = st.median(vals)
        if med >= OVERRUN:
            advice.append(f"{NAMES[kind].capitalize()} end ~{(med - 1) * 100:.0f} % longer than planned (median of "
                          f"{len(vals)}): keep the plan as the ceiling with the bonus cap written in the label (P41); "
                          "don't raise the planned km to match.")
        elif med <= UNDERRUN:
            advice.append(f"{NAMES[kind].capitalize()} end ~{(1 - med) * 100:.0f} % shorter than planned (median of "
                          f"{len(vals)}): check heat, fuel and time before planning them longer (P38).")
    extra, rest = extra_runs(rows)
    if rest >= MIN_N and extra >= 2 and extra / rest >= 0.25:
        advice.append(f"Runs on planned rest or gym-only days: {extra} of {rest}. They count as over-plan; keep one "
                      "day a week with neither a run nor a lift (P41).")
    return advice


# ── logistic skip model (needs 8+ weeks of rows and some skips) ─────────────
def _x(weekday: str, planned_type: str, planned_km: float, km_mean: float, km_sd: float) -> np.ndarray:
    hard = planned_type in ("tempo", "intervals")
    return np.array([1.0] + [float(weekday == d) for d in WEEKDAYS]
                    + [float(planned_type == "long_run"), float(hard), (planned_km - km_mean) / km_sd])


def skip_probability(rows: list[dict], weekday: str, planned_type: str, planned_km: float,
                     min_weeks: int = 8, l2: float = 1.0) -> float | None:
    """P(skip) for a planned run from a small L2-regularised logistic regression on weekday, type and planned
    km. None until the rows span min_weeks weeks with at least 3 skips and 3 done (one class can't be learned)."""
    data = [r for r in _runs(rows) if num(r.get("planned_km")) and r.get("date")]
    if not data:
        return None
    days = sorted(date.fromisoformat(r["date"][:10]) for r in data)
    y = np.array([0.0 if done_value(r["done"]) else 1.0 for r in data])
    if (days[-1] - days[0]).days < 7 * min_weeks - 7 or y.sum() < 3 or (1 - y).sum() < 3:
        return None
    kms = np.array([num(r["planned_km"]) for r in data])
    mean, sd = float(kms.mean()), float(kms.std()) or 1.0
    X = np.array([_x(r["weekday"], r["planned_type"], num(r["planned_km"]), mean, sd) for r in data])
    w = np.zeros(X.shape[1])
    penalty = np.full(X.shape[1], l2)
    penalty[0] = 0.0                                     # no shrinkage on the intercept
    for _ in range(50):                                  # Newton steps (IRLS)
        p = 1 / (1 + np.exp(-X @ w))
        grad = X.T @ (p - y) + penalty * w
        H = X.T @ (X * (p * (1 - p))[:, None]) + np.diag(penalty) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(H, grad)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return float(1 / (1 + np.exp(-_x(weekday, planned_type, planned_km, mean, sd) @ w)))
