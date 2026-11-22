"""Heat- and hill-corrected running efficiency, learned from the athlete's own runs (build_plan T21, ROADMAP B1).

Efficiency (EF) = metres per minute per heartbeat (speed / average HR), the measure behind the tracker's
"Pace at 150 bpm" and the run card. Heat and climbing both lower it, so a raw EF can't say whether a run was
good for its conditions. This model learns from the athlete's steady outdoor runs how much each one costs *him*:

    EF ~ 1 + heat_index_c + climb_per_km [+ weeks]      numpy weighted least squares, small ridge term

With dated rows a time trend (weeks) is added, so fitness gains are not mistaken for cooler weather, and the
trend itself is the heat- and hill-corrected fitness change. With half_life_days, recent runs weigh more, so
the heat cost follows his acclimatisation. A feature with no variance is dropped (it can't be learned).

    fit(rows, min_rows=20, today=None, half_life_days=None) -> model | None
    expected_ef(model, heat_index_c, climb_per_km, day=None) -> EF expected for those conditions
    residual_pct(model, row) -> % above (+, better) or below (-) the expected EF for that run's conditions
    describe(model) -> plain-language lines for the weekly model report

Rows: {"ef", "heat_index_c", "climb_per_km"[, "date"]}; model_utils.steady_runs() makes them from the ledger.
The model describes; it never changes a gate or a plan.
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
from core_common import today_pht  # noqa: E402
from model_utils import clear, num, predict, wls  # noqa: E402

FEATURES = ("heat_index_c", "climb_per_km")
MIN_ROWS = 20
REF_HR = 150                      # the tracker's "Pace at 150 bpm"
REF_HEAT, REF_CLIMB = 29.0, 4.0   # a typical tropical morning, gently rolling route (T12b defaults)
MODEL_FILE = os.path.join(os.path.dirname(HERE), "data", "models", "efficiency.json")


def _day(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def fit(rows: list[dict], min_rows: int = MIN_ROWS, today: date | None = None,
        half_life_days: float | None = None) -> dict | None:
    data = [dict(r) for r in rows if num(r.get("ef")) and all(num(r.get(f)) is not None for f in FEATURES)]
    if len(data) < min_rows:
        return None
    feats, ref, weights = list(FEATURES), None, None
    days = [_day(r.get("date")) for r in data]
    if all(days):
        ref = today or max(days)
        for r, d in zip(data, days):
            r["weeks"] = (d - ref).days / 7
        feats.append("weeks")
        if half_life_days:
            weights = [0.5 ** (max((ref - d).days, 0) / half_life_days) for d in days]
    model = wls(data, "ef", feats, weights)
    if model is None:
        return None
    model.update(kind="efficiency", ref_date=ref.isoformat() if ref else None, half_life_days=half_life_days)
    return model


def expected_ef(model: dict, heat_index_c: float, climb_per_km: float, day=None) -> float:
    """Expected EF for these conditions, at the fitness of `day` (default: the fit date, i.e. current form)."""
    weeks = 0.0
    d, ref = _day(day), _day(model.get("ref_date"))
    if d and ref:
        weeks = (d - ref).days / 7
    return predict(model, heat_index_c=heat_index_c, climb_per_km=climb_per_km, weeks=weeks)


def residual_pct(model: dict, row: dict) -> float:
    """How much better (+) or worse (-) than expected this run was, in %, for its heat and climb."""
    ef = num(row.get("ef"))
    if ef is None and num(row.get("speed_mps")) and num(row.get("avg_hr")):
        ef = num(row["speed_mps"]) * 60 / num(row["avg_hr"])
    exp = expected_ef(model, num(row.get("heat_index_c")), num(row.get("climb_per_km")), row.get("date"))
    return (ef / exp - 1) * 100


def pace_at(ef: float, hr: float = REF_HR) -> float:
    """Seconds per km at this HR for an EF in m/min per beat."""
    return 1000 / (ef * hr) * 60


def fmt_pace(sec: float) -> str:
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def describe(model: dict) -> list[str]:
    """What the model learned, in plain words, with how sure it is."""
    base = expected_ef(model, REF_HEAT, REF_CLIMB)
    lines = [f"Expected pace at {REF_HR} bpm on a {REF_HEAT:.0f} C heat index, {REF_CLIMB:.0f} m/km route: "
             f"{fmt_pace(pace_at(base))}/km (EF {base:.2f})."]

    def cost(feature, step, what):
        c = model["coef"].get(feature)
        if c is None:
            return f"{what}: not learnable yet (no variation in these runs)."
        pct = c * step / base * 100
        secs = pace_at(base + c * step) - pace_at(base)
        sure = "clear" if clear(model, feature) else "not clear yet (within the noise)"
        return f"{what}: {pct:+.1f} % efficiency, ~{secs:+.0f} s/km at {REF_HR} bpm ({sure})."

    lines.append(cost("heat_index_c", 1.0, "Each +1 C of heat index"))
    lines.append(cost("climb_per_km", 10.0, "Each +10 m of climb per km"))
    if "weeks" in model["coef"]:
        lines.append(cost("weeks", 4.35, "Fitness trend per month, heat- and hill-corrected"))
    r2 = model.get("r2")
    lines.append(f"Learned from {model['n']} steady outdoor runs (effective {model['n_eff']:.0f} with recency weights)"
                 + (f"; explains {r2 * 100:.0f} % of the run-to-run variation" if r2 is not None else "")
                 + f"; typical scatter +/-{model['resid_sd'] / base * 100:.1f} %.")
    return lines


def save(model: dict, path: str | None = None, generated: date | None = None) -> None:
    path = path or MODEL_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keep = {k: model[k] for k in ("kind", "intercept", "coef", "se", "means", "range", "n", "n_eff", "r2",
                                  "resid_sd", "ref_date", "half_life_days", "dropped")}
    keep["generated"] = (generated or today_pht()).isoformat()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, indent=1)
        fh.write("\n")


def load(path: str | None = None, today: date | None = None, max_age_days: int = 21) -> dict | None:
    """The saved model, or None when missing, unreadable or older than max_age_days."""
    try:
        with open(path or MODEL_FILE, encoding="utf-8") as fh:
            model = json.load(fh)
        made = date.fromisoformat(model["generated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if ((today or today_pht()) - made).days > max_age_days:
        return None
    return model
