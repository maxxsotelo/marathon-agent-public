"""Weekly retrain of the athlete's personal models (Sundays in the morning check, or by hand). Repo files only: no Garmin.

    python scripts/learn_weekly.py [--today YYYY-MM-DD] [--ledger DIR] [--dry-run]

Reads the training ledger (data/ledger/, scripts/ledger.py) and refits three small models on his own data:
  1. Efficiency (T21): what heat and hills cost him, and his heat- and hill-corrected fitness trend
     -> data/models/efficiency.json (the run card shows "x % vs expected for these conditions").
  2. Pace (T12b): the pace he runs at each HR zone now -> data/pace_bands.json (the workout generator adds it
     as a secondary target; HR stays primary, KB Rule 9; each end moves at most 5 s/km a week).
  3. Habits (T19): where planned runs get skipped, extended or added -> advice for Sunday planning.
Writes reports/models/YYYY-MM-DD.md in plain language (what each model learned, from how much data, how sure),
prints it, and ends with a [MODELS] summary the morning check shows.

Models describe and suggest. They never change a gate, a hard limit, a plan or the athlete's rules (CLAUDE.md), and a
model without enough data says so instead of guessing (Rule 1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import efficiency_model as em  # noqa: E402
import habit_model as hm  # noqa: E402
import pace_model as pm  # noqa: E402
from core_common import today_pht  # noqa: E402
from ledger import LEDGER, read_rows  # noqa: E402
from model_utils import steady_runs  # noqa: E402

REPORTS = os.path.join(ROOT, "reports", "models")
EFF_HALF_LIFE = 120          # days: the heat cost follows acclimatisation over a season
OLD_BAND_MAX_AGE = 28        # days: last week's band caps this week's move only if it is this recent


def efficiency_section(runs: list[dict], today: date):
    usable = [r for r in runs if r["heat_index_c"] is not None and r["climb_per_km"] is not None]
    model = em.fit(usable, today=today, half_life_days=EFF_HALF_LIFE)
    lines = ["## 1. Heat, hills and fitness (efficiency model)", ""]
    if model is None:
        lines += [f"- Waiting for data: {len(usable)} steady outdoor runs with temperature and elevation in the "
                  f"ledger; the model needs {em.MIN_ROWS}.", ""]
        return lines, None, [f"Efficiency model: waiting for data ({len(usable)} of {em.MIN_ROWS} runs)."]
    lines += [f"- {t}" for t in em.describe(model)]
    week = [r for r in usable if r["date"] and r["date"] > (today - timedelta(days=7)).isoformat()]
    if week:
        lines += ["", "Last 7 days, against what the model expects for each run's heat and climb:", "",
                  "| date | km | HR | heat index | climb m/km | vs expected |", "|---|---|---|---|---|---|"]
        for r in week:
            lines.append(f"| {r['date']} | {r['km']:.1f} | {r['avg_hr']:.0f} | {r['heat_index_c']:.0f} C | "
                         f"{r['climb_per_km']:.0f} | {em.residual_pct(model, r):+.1f} % |")
    lines.append("")
    heat = model["coef"].get("heat_index_c")
    base = em.expected_ef(model, em.REF_HEAT, em.REF_CLIMB)
    parts = []
    if heat is not None:
        secs = em.pace_at(base + heat) - em.pace_at(base)
        parts.append(f"heat ~{secs:+.0f} s/km per +1 C" + ("" if em.clear(model, "heat_index_c") else " (not clear yet)"))
    if "weeks" in model["coef"]:
        parts.append(f"corrected fitness {model['coef']['weeks'] * 4.35 / base * 100:+.1f} %/month"
                     + ("" if em.clear(model, "weeks") else " (not clear yet)"))
    if week:
        mean = sum(em.residual_pct(model, r) for r in week) / len(week)
        parts.append(f"this week's runs {mean:+.1f} % vs expected")
    return lines, model, ["Efficiency: " + "; ".join(parts) + "."]


def pace_section(runs: list[dict], today: date, old: dict):
    lines = ["## 2. Pace bands (pace model; HR stays the primary target)", ""]
    model = pm.fit_hr_speed(runs, today)
    if model is None:
        recent = sum(1 for r in runs if r["date"] and r["date"] >= (today - timedelta(days=pm.RECENT_DAYS)).isoformat())
        lines += [f"- Not updated: {len(runs)} steady outdoor runs ({recent} in the last {pm.RECENT_DAYS} days); the model "
                  f"needs 8, with {pm.MIN_RECENT} recent. Workouts carry HR targets only until it has them.", ""]
        return lines, None, ["Pace bands: not updated (not enough recent steady runs); workouts use HR only."]
    old_ok = old.get("generated") and (today - date.fromisoformat(old["generated"][:10])).days <= OLD_BAND_MAX_AGE
    old_bands = (old.get("bands") or {}) if old_ok else {}
    cover = pm.hr_coverage(runs, today)
    bands, rows, short = {}, [], []
    for key, (lo, hi) in pm.BANDS.items():
        if not pm.band_supported(lo, hi, cover):
            seen = f"averaged {cover[0]:.0f}-{cover[1]:.0f} bpm" if cover else "are missing"
            rows.append(f"| {key} | {lo}-{hi} | HR only: the last 90 days' steady runs {seen}, so this zone would be "
                        "a guess | | |")
            short.append(f"{key} HR only")
            continue
        raw = pm.pace_band(model, lo, hi)
        if raw is None:
            rows.append(f"| {key} | {lo}-{hi} | HR only: no usable band this week | | |")
            continue
        prev = old_bands.get(key)
        prev_t = (prev["fast_s_per_km"], prev["slow_s_per_km"]) if prev else None
        fast, slow = (round(x) for x in pm.cap_change(raw, prev_t))
        bands[key] = {"hr": [lo, hi], "fast_s_per_km": fast, "slow_s_per_km": slow,
                      "pace": f"{pm.fmt_pace(fast)}-{pm.fmt_pace(slow)}", "learned_s_per_km": [raw[0], raw[1]],
                      "capped": bool(prev_t) and (round(raw[0]), round(raw[1])) != (fast, slow)}
        last = f"{pm.fmt_pace(prev_t[0])}-{pm.fmt_pace(prev_t[1])}" if prev_t else "none"
        note = " (moved the 5 s/km weekly maximum)" if bands[key]["capped"] else ""
        rows.append(f"| {key} | {lo}-{hi} | {bands[key]['pace']}/km{note} | {pm.fmt_pace(raw[0])}-{pm.fmt_pace(raw[1])} | {last} |")
        short.append(f"{key} {bands[key]['pace']}")
    lines += [f"At a {pm.DEFAULT_HEAT:.0f} C heat index on a {pm.DEFAULT_CLIMB:.0f} m/km route, from {model['n']} steady "
              f"outdoor runs (effective {model['n_eff']:.0f}: the last 3 weeks count most).", "",
              "| band | HR | on the watch | learned this week | last week |", "|---|---|---|---|---|", *rows, ""]
    easy = pm.pace_band(model, *pm.BANDS["easy"], heat_index_c=pm.DEFAULT_HEAT + 4)
    base = pm.pace_band(model, *pm.BANDS["easy"])
    if easy and base and "heat_index_c" in model["coef"]:
        lines += [f"- On a hotter day ({pm.DEFAULT_HEAT + 4:.0f} C heat index) the same HR runs ~{easy[0] - base[0]:+.0f} s/km: "
                  "the HR target already allows for that, so slow down rather than chase the band.", ""]
    payload = {"generated": today.isoformat(), "source": "scripts/learn_weekly.py (pace model, build_plan T12b)",
               "conditions": {"heat_index_c": pm.DEFAULT_HEAT, "climb_per_km": pm.DEFAULT_CLIMB},
               "half_life_days": model["half_life_days"], "n_runs": model["n"], "n_eff": model["n_eff"],
               "bands": bands} if bands else None
    return lines, payload, ["Pace bands (secondary to HR): " + ", ".join(short) + "."]


def habit_section(plan_rows: list[dict]):
    lines = ["## 3. Habits (plan vs what happened)", ""]
    runs = hm._runs(plan_rows)
    if len(runs) < hm.MIN_N:
        lines += [f"- Waiting for data: {len(runs)} planned runs with a known outcome in data/ledger/plan.csv.", ""]
        return lines, ["Habits: waiting for data."]
    done = sum(hm.done_value(r["done"]) for r in runs)
    lines.append(f"- Planned runs done: {done} of {len(runs)}.")
    for kind in hm.RUN_TYPES:
        vals = hm.ratios(plan_rows, kind)
        if vals:
            med = hm.overrun_ratio(plan_rows, kind)
            lines.append(f"- {hm.NAMES[kind].capitalize()}: median {med * 100:.0f} % of the planned km ({len(vals)} with a known plan).")
    extra, rest = hm.extra_runs(plan_rows)
    if rest:
        lines.append(f"- Planned rest or gym-only days with a run: {extra} of {rest}.")
    advice = hm.planning_advice(plan_rows)
    lines += ["", "Advice for Sunday planning:"] + [f"- {a}" for a in advice or ["No pattern to plan around yet."]]
    skips = len(runs) - done
    p = hm.skip_probability(plan_rows, "Thu", "easy", 6.0)
    lines += ["", f"- Skip model: Thursday 6 km easy run, chance of a skip ~{p * 100:.0f} %." if p is not None else
              f"- Skip model: not trained ({skips} skipped of {len(runs)}; it needs 8 weeks with 3+ skips). "
              "The pattern so far is doing more, not less.", ""]
    return lines, ["Habits: " + (advice[0] if advice else "no pattern to plan around yet.")]


def build(ledger_dir: str, today: date, old_bands: dict):
    acts = read_rows(os.path.join(ledger_dir, "activities.csv"))
    plan = read_rows(os.path.join(ledger_dir, "plan.csv"))
    runs = [r for r in steady_runs(acts) if r["date"] and r["date"] <= today.isoformat()]
    e_lines, e_model, e_sum = efficiency_section(runs, today)
    p_lines, bands, p_sum = pace_section(runs, today, old_bands)
    h_lines, h_sum = habit_section(plan)
    head = [f"# What the models learned: week ending {today:%a %b %d, %Y}", "",
            f"From the training ledger: {len(acts)} activities, {len(runs)} steady outdoor runs, {len(plan)} planned days. "
            "Retrained every Sunday (scripts/learn_weekly.py). Models describe and suggest; they never change a gate, "
            "a limit or a plan.", ""]
    report = "\n".join(head + e_lines + p_lines + h_lines)
    return report, e_model, bands, e_sum + p_sum + h_sum


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", help="YYYY-MM-DD (default: today, PHT)")
    ap.add_argument("--ledger", default=str(LEDGER), help="ledger directory (default data/ledger)")
    ap.add_argument("--dry-run", action="store_true", help="print only; write nothing")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else today_pht()
    report, e_model, bands, summary = build(args.ledger, today, pm.read_bands())
    path = os.path.join(REPORTS, f"{today.isoformat()}.md")
    if not args.dry_run:
        os.makedirs(REPORTS, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        if e_model:
            em.save(e_model, generated=today)
        if bands:
            os.makedirs(os.path.dirname(pm.PACE_BANDS_FILE), exist_ok=True)
            with open(pm.PACE_BANDS_FILE, "w", encoding="utf-8") as fh:
                json.dump(bands, fh, indent=1)
                fh.write("\n")
    print(report)
    print("\n[MODELS]")
    for s in summary + ([f"Report: reports/models/{today.isoformat()}.md"] if not args.dry_run else []):
        print(f"- {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
