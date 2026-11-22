"""Small, explainable regression helpers shared by the personal models (numpy only, no scikit-learn).

wls(rows, target, features, weights=None, ridge=1e-3) fits target ~ 1 + features by weighted least squares.
Features are standardised first; a small ridge term keeps a short or nearly flat sample from making the fit
singular, and a feature with no variance in the sample is dropped (it can't be learned from these rows).
Each coefficient comes with a standard error, so a report can say "not clear yet" instead of turning noise
into a finding (Rule 1).

steady_runs(activities) picks the ledger runs a pace or efficiency model can learn from: outdoor, 5 km+,
average HR 130-178 (the athlete's easy and long runs sit in Z2 162-174, KB Rule 9), not interval-like (anaerobic TE
under 2.0) and not a race or time trial.
"""

from __future__ import annotations

import math
import re

import numpy as np

STEADY_HR = (130, 178)
STEADY_MIN_KM = 5.0
NOT_STEADY = re.compile(r"\b(race|time trial|tt|5k tt|interval|intervals|repeats?|vo2)\b", re.I)


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def wls(rows: list[dict], target: str, features: list[str] | tuple, weights=None, ridge: float = 1e-3) -> dict | None:
    features = list(features)
    idx = [i for i, r in enumerate(rows)
           if num(r.get(target)) is not None and all(num(r.get(f)) is not None for f in features)
           and (weights is None or (num(weights[i]) or 0) > 0)]
    if len(idx) < 3:
        return None
    w = np.array([1.0 if weights is None else float(weights[i]) for i in idx])
    y = np.array([num(rows[i][target]) for i in idx])
    X = np.array([[num(rows[i][f]) for f in features] for i in idx], dtype=float).reshape(len(idx), len(features))
    wsum = float(w.sum())
    n_eff = wsum ** 2 / float((w ** 2).sum())            # Kish effective sample size (recency weights shrink it)
    mu = (w @ X) / wsum if features else np.zeros(0)
    sd = np.sqrt((w @ (X - mu) ** 2) / wsum) if features else np.zeros(0)
    used = [j for j in range(len(features)) if sd[j] > 1e-9 * max(1.0, abs(mu[j]))]
    ym = float(w @ y / wsum)
    coef_z = np.zeros(len(used))
    Z = np.zeros((len(idx), 0))
    if used:
        Z = (X[:, used] - mu[used]) / sd[used]
        A = Z.T @ (w[:, None] * Z) + ridge * wsum * np.eye(len(used))
        coef_z = np.linalg.solve(A, Z.T @ (w * (y - ym)))
    resid = y - ym - Z @ coef_z
    ss_res = float(w @ resid ** 2)
    ss_tot = float(w @ (y - ym) ** 2)
    dof = n_eff - len(used) - 1
    se = {}
    if used and dof > 0:
        s2 = ss_res / wsum * n_eff / dof                      # residual variance, per effective observation
        cov = s2 * np.linalg.inv(Z.T @ (w[:, None] * Z) * (n_eff / wsum) + ridge * n_eff * np.eye(len(used)))
        se = {features[used[k]]: float(math.sqrt(max(cov[k, k], 0.0)) / sd[used[k]]) for k in range(len(used))}
    coef = {features[used[k]]: float(coef_z[k] / sd[used[k]]) for k in range(len(used))}
    return {"intercept": ym - sum(coef[f] * float(mu[features.index(f)]) for f in coef), "coef": coef, "se": se,
            "dropped": [f for j, f in enumerate(features) if j not in used], "n": len(idx), "n_eff": round(n_eff, 1),
            "r2": (1 - ss_res / ss_tot) if ss_tot > 0 else None, "resid_sd": math.sqrt(ss_res / wsum),
            "means": {f: float(mu[j]) for j, f in enumerate(features)},
            "range": {f: [float(X[:, j].min()), float(X[:, j].max())] for j, f in enumerate(features)}}


def predict(model: dict, **values) -> float:
    """intercept + sum(coef x value). A feature the model dropped (no variance) contributes nothing; a feature
    not given is taken at its sample mean."""
    return model["intercept"] + sum(c * float(values[f] if values.get(f) is not None else model["means"][f])
                                    for f, c in model["coef"].items())


def clear(model: dict, feature: str, z: float = 2.0) -> bool:
    """True when the effect is at least z standard errors from zero."""
    c, s = model["coef"].get(feature), model.get("se", {}).get(feature)
    return c is not None and s is not None and s > 0 and abs(c) >= z * s


def steady_runs(activities: list[dict]) -> list[dict]:
    """Ledger activity rows -> model rows {date, km, avg_hr, speed_mps, ef, heat_index_c, climb_per_km}."""
    from core_common import RUN_TYPE_KEYS
    out = []
    for a in activities:
        if a.get("type") not in RUN_TYPE_KEYS or str(a.get("treadmill")) == "1":
            continue
        km, hr, mins = num(a.get("km")), num(a.get("avg_hr")), num(a.get("moving_min"))
        speed = num(a.get("avg_speed_mps")) or (km * 1000 / (mins * 60) if km and mins else None)
        if not (km and hr and speed) or km < STEADY_MIN_KM or not STEADY_HR[0] <= hr <= STEADY_HR[1]:
            continue
        if (num(a.get("anaerobic_te")) or 0) >= 2.0 or NOT_STEADY.search(a.get("name") or ""):
            continue
        out.append({"date": a.get("date"), "km": km, "avg_hr": hr, "speed_mps": speed, "ef": speed * 60 / hr,
                    "heat_index_c": num(a.get("heat_index_c")), "climb_per_km": num(a.get("climb_per_km")),
                    "name": a.get("name") or ""})
    return sorted(out, key=lambda r: r["date"] or "")
