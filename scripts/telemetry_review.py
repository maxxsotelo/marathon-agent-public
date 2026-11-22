"""Deep telemetry and history rundown: one deterministic packet any model can regenerate.

    python scripts/telemetry_review.py [--out DIR] [--today YYYY-MM-DD]

No Garmin login: it reads only files in the repo (the morning check refreshes data/dashboard/
with Garmin every day). Same input, same output, whichever model runs it. Writes to DIR
(default reviews/<YYYY-MM>-deep/packet/):

  rundown.md          every section below, with auto-flags at the top
  weekly.csv          one row per Monday week: km, runs, longest, hard, lifting days, sleep, HRV, RHR, EA
  run_biomech.csv     one row per run analysed by core_run_analysis.py (scratch/run_analysis_*.md)
  adherence.csv       plan vs Garmin for the days where the week plan still holds the plan

Sections: coverage, volume and consistency, long runs, intensity, aerobic efficiency, recovery
(HRV, RHR, sleep), load (ACWR, monotony), fuel (EA, macros), strength, behaviour (plan
adherence, timing, weekdays, decisions), exploratory correlations, biomechanics.
Nothing is estimated (Rule 1): a metric with too little data says so instead of a number.
Used by docs/DEEP_REVIEW.md (the deep-review playbook for Claude and Gemini).
"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import re
import runpy
import statistics as st
import sys
from collections import Counter, OrderedDict, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_common import LEAN_MASS_KG, today_pht  # noqa: E402

Z1_TOP, Z2_TOP = 161, 174            # KB zones: Z1 < 162, Z2 162-174
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIN_CORR_N = 10                      # correlations below this sample size are not printed


# ── small helpers ────────────────────────────────────────────────────────────

def n(v):
    """A float or None for '' / None / text."""
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def median(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def f(v, nd=1, unit=""):
    return "–" if v is None else f"{v:.{nd}f}{unit}"


def pct(part, whole):
    return None if not whole else 100.0 * part / whole


def pace(sec_per_km):
    if sec_per_km is None:
        return "–"
    s = int(round(sec_per_km))
    return f"{s // 60}:{s % 60:02d}"


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})


def table(header: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None, len(pairs)
    a, b = zip(*pairs)
    if len(set(a)) < 2 or len(set(b)) < 2:
        return None, len(pairs)
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in pairs)
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return (num / den if den else None), len(pairs)


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None, len(pairs)
    a, b = zip(*pairs)
    return pearson(ranks(list(a)), ranks(list(b)))[0], len(pairs)


# ── loading ──────────────────────────────────────────────────────────────────

def load_days(root: Path) -> list[dict]:
    rows = read_csv(root / "data" / "dashboard" / "days.csv")
    return sorted(rows, key=lambda r: r["date"])


def load_runs(root: Path) -> list[dict]:
    return sorted(read_csv(root / "data" / "dashboard" / "runs.csv"), key=lambda r: (r["date"], r.get("start") or ""))


def parse_history(root: Path) -> dict:
    """Monthly trends (incl. fasted-weight monthly averages) and the main-lift table."""
    path = root / "brain_athlete_history.md"
    out = {"monthly": [], "generated": ""}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8")
    if m := re.search(r"generated (\d{4}-\d\d-\d\d)", text):
        out["generated"] = m.group(1)
    i = text.find("## 4. Monthly trends")
    rows = []
    for line in text[i:].splitlines()[1:] if i >= 0 else []:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                rows.append(cells)
        elif rows:
            break
    if rows:
        head = rows[0]
        for r in rows[1:]:
            out["monthly"].append(dict(zip(head, r)))
    return out


def parse_run_analyses(root: Path) -> list[dict]:
    """Key metrics from every scratch/run_analysis_<id>_<YYYYMMDD>.md."""
    rows = []
    for path in sorted(glob.glob(str(root / "scratch" / "run_analysis_*_*.md"))):
        m = re.search(r"run_analysis_(\d+)_(\d{8})\.md$", path)
        if not m:
            continue
        text = Path(path).read_text(encoding="utf-8", errors="replace")

        def cell(label):
            mm = re.search(r"\|\s*\*\*" + re.escape(label) + r"\*\*\s*\|\s*([^|]+)\|", text)
            return mm.group(1).strip() if mm else ""

        def num_in(s):
            mm = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
            return float(mm.group(0)) if mm else None

        title = (text.splitlines() or [""])[0].replace("# Exhaustive Run Analysis:", "").strip()
        d = m.group(2)
        dist = num_in(cell("Total Distance"))
        pace_txt = cell("Average Pace")
        pm = re.match(r"(\d+):(\d\d)", pace_txt)
        heat = re.search(r"Heat Index during session: \*\*([\d.]+)", text)
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "activity_id": m.group(1), "name": title[:80],
            "treadmill": int(bool(re.search(r"treadmill", title, re.I))),
            "km": dist, "pace_s": (int(pm.group(1)) * 60 + int(pm.group(2))) if pm else None,
            "avg_hr": num_in(cell("Average HR")), "aerobic_te": num_in(cell("Aerobic Training Effect")),
            "decoupling_pct": num_in(cell("PA:HR Decoupling %")), "hr_drift": num_in(cell("Absolute HR Drift")),
            "trimp": num_in(cell("Total TRIMP (Banister)")), "epoc": num_in(cell("Session EPOC Estimate")),
            "cadence": num_in(cell("Avg Cadence")), "gct_ms": num_in(cell("Avg GCT")),
            "vert_ratio": num_in(cell("Avg Vertical Ratio")), "heat_index": float(heat.group(1)) if heat else None,
        })
    return rows


def load_plans(root: Path) -> dict[str, dict]:
    days = {}
    files = sorted(glob.glob(str(root / "archive" / "week_plans" / "*.py")))
    cur = root / "brain_current_week_plan.py"
    if cur.exists():
        files.append(str(cur))
    for fpath in files:
        try:
            plan = runpy.run_path(fpath).get("plan") or {}
        except Exception:
            continue
        for d, e in (plan.get("days") or {}).items():
            e = dict(e)
            e["_file"] = os.path.basename(fpath)
            days[d] = e
    return days


def parse_nutrition(root: Path) -> list[dict]:
    path = root / "brain_nutrition_log.md"
    out, seen = [], set()
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (\d{4}-\d\d-\d\d) \| (\d+) \| ([\d.?]+) \| ([\d.?]+) \| ([\d.?]+) \|", line)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append({"date": m.group(1), "kcal": n(m.group(2)), "protein": n(m.group(3)),
                    "carbs": n(m.group(4)), "fat": n(m.group(5))})
    return sorted(out, key=lambda r: r["date"])


def parse_decisions(root: Path) -> list[dict]:
    path = root / "brain_decision_log.md"
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (\d{4}-\d\d-\d\d[^|]*) \| (\d{4}-\d\d-\d\d) \| ([^|]*) \| ([^|]*) \| ([^|]*) \| ([^|]*) \|", line)
        if m:
            out.append({"logged": m.group(1).strip(), "date": m.group(2), "session": m.group(3).strip(),
                        "gate": m.group(4).strip(), "decision": m.group(5).strip(), "outcome": m.group(6).strip()})
    return out


def parse_strength_prs(root: Path) -> list[str]:
    """Dates of rows marked **PR** in brain_strength_records.md."""
    path = root / "brain_strength_records.md"
    if not path.exists():
        return []
    return [m.group(1) for m in re.finditer(r"^\| (\d{4}-\d\d-\d\d) \|[^\n]*\*\*PR\*\*", path.read_text(encoding="utf-8"), re.M)]


# ── computations ─────────────────────────────────────────────────────────────

def weekly(days: list[dict], runs: list[dict]) -> list[dict]:
    weeks: "OrderedDict[str, dict]" = OrderedDict()
    for r in days:
        if r["status"] not in ("done", "today"):
            continue
        wk = r["week_start"]
        w = weeks.setdefault(wk, {"week": wk, "label": r.get("week_label") or "", "days": 0, "km": 0.0, "runs": 0,
                                  "longest": 0.0, "hard": 0, "lift_days": 0, "_sleep": [], "_hrv": [], "_rhr": [],
                                  "_ea": [], "_trimp": []})
        w["days"] += 1
        w["km"] += n(r["km"]) or 0
        if n(r.get("lifted")) == 1:
            w["lift_days"] += 1
        for k, key in (("_sleep", "sleep_h"), ("_hrv", "hrv"), ("_rhr", "rhr"), ("_ea", "ea"), ("_trimp", "trimp")):
            if n(r.get(key)) is not None:
                w[k].append(n(r[key]))
    for run in runs:
        wk = monday(date.fromisoformat(run["date"])).isoformat()
        if wk in weeks:
            w = weeks[wk]
            w["runs"] += 1
            w["longest"] = max(w["longest"], n(run["km"]) or 0)
            if run.get("kind") == "hard":
                w["hard"] += 1
    out = []
    for w in weeks.values():
        out.append({**{k: v for k, v in w.items() if not k.startswith("_")},
                    "km": round(w["km"], 1), "sleep_h": f(mean(w["_sleep"]), 2) if w["_sleep"] else "",
                    "hrv": f(mean(w["_hrv"]), 0) if w["_hrv"] else "", "rhr": f(mean(w["_rhr"]), 1) if w["_rhr"] else "",
                    "ea": f(mean(w["_ea"]), 1) if w["_ea"] else "",
                    "trimp": round(sum(w["_trimp"]), 0) if w["_trimp"] else "",
                    "monotony": f(st.mean(w["_trimp"]) / st.pstdev(w["_trimp"]), 2)
                    if len(w["_trimp"]) >= 5 and st.pstdev(w["_trimp"]) > 0 else ""})
    return out


def acwr_series(days: list[dict]) -> list[tuple[str, float]]:
    """Mechanical ACWR (7-day km / (28-day km / 4)) for days whose 28 days all have data."""
    rows = [r for r in days if r["status"] in ("done", "today")]
    out = []
    for i in range(27, len(rows)):
        win = rows[i - 27:i + 1]
        if any(n(r["km"]) is None for r in win):
            continue
        if (date.fromisoformat(win[-1]["date"]) - date.fromisoformat(win[0]["date"])).days != 27:
            continue
        km28 = sum(n(r["km"]) for r in win)
        km7 = sum(n(r["km"]) for r in win[-7:])
        if km28 > 0:
            out.append((rows[i]["date"], km7 / (km28 / 4)))
    return out


def plan_holds_actual(entry: dict) -> bool:
    """A plan day written over with what was done: the `completed` flag, or COMPLETED in its label or
    session text (older week plans only marked it there). Its run_km is the actual, not the plan."""
    text = f"{entry.get('label', '')} {entry.get('session', '')}"
    return bool(entry.get("completed")) or "COMPLETED" in text


def adherence(plans: dict[str, dict], days: list[dict]) -> list[dict]:
    """Plan vs Garmin for days whose plan was not overwritten by the actuals.

    A plan day marked completed (flag, or COMPLETED in its text) holds the actual km written after the
    fact, so it is not a plan any more; those days are skipped. Outcome: as planned (within 15 % or 1 km), extended,
    shortened, skipped (planned run, no run), extra (no run planned, ran)."""
    by_date = {r["date"]: r for r in days}
    out = []
    for d, e in sorted(plans.items()):
        day = by_date.get(d)
        if not day or day["status"] != "done" or plan_holds_actual(e):
            continue
        planned, actual = n(e.get("run_km")), n(day["km"])
        if planned is None or actual is None:
            continue
        if planned == 0 and actual == 0:
            outcome = "rest as planned"
        elif planned == 0:
            outcome = "extra run"
        elif actual == 0:
            outcome = "skipped"
        elif abs(actual - planned) <= max(1.0, 0.15 * planned):
            outcome = "as planned"
        else:
            outcome = "extended" if actual > planned else "shortened"
        out.append({"date": d, "weekday": WEEKDAYS[date.fromisoformat(d).weekday()], "planned_km": planned,
                    "actual_km": round(actual, 2), "outcome": outcome, "plan_file": e.get("_file", "")})
    return out


# ── report ───────────────────────────────────────────────────────────────────

def build(root: Path, today: date) -> dict:
    days, runs = load_days(root), load_runs(root)
    hist = parse_history(root)
    biomech = parse_run_analyses(root)
    plans = load_plans(root)
    nutrition = parse_nutrition(root)
    decisions = parse_decisions(root)
    pr_dates = parse_strength_prs(root)
    lifts = read_csv(root / "data" / "dashboard" / "lifts.csv")
    fitness = {r["metric"]: r for r in read_csv(root / "data" / "dashboard" / "fitness.csv")}
    weeks = weekly(days, runs)
    acwr = acwr_series(days)
    adh = adherence(plans, days)

    done = [r for r in days if r["status"] in ("done", "today")]
    L: list[str] = []
    flags: list[str] = []

    # 0. coverage
    def span(rows, key):
        ds = [r["date"] for r in rows if n(r.get(key)) is not None]
        return (f"{ds[0]} to {ds[-1]} ({len(ds)} days)" if ds else "none")
    L += ["## 0. Coverage", ""]
    L += table(["Source", "Range / count"], [
        ["Daily run km (Garmin export, data/dashboard/days.csv)", span(done, "km")],
        ["Runs (merged split runs, runs.csv)", f"{len(runs)} runs, {runs[0]['date'] if runs else '–'} to {runs[-1]['date'] if runs else '–'}"],
        ["Overnight HRV", span(done, "hrv")], ["Resting HR", span(done, "rhr")],
        ["Sleep hours", span(done, "sleep_h")], ["Energy availability", span(done, "ea")],
        ["HRV floor (Garmin balanced-low)", span(done, "hrv_floor")],
        ["Run analyses (scratch/run_analysis_*.md)", f"{len(biomech)} runs, {biomech[0]['date'] if biomech else '–'} to {biomech[-1]['date'] if biomech else '–'}"],
        ["Monthly trends (brain_athlete_history.md)", f"{len(hist['monthly'])} months (generated {hist['generated'] or '–'})"],
        ["Week plans", f"{len({e['_file'] for e in plans.values()})} files, {len(plans)} days"],
        ["Nutrition log rows", f"{len(nutrition)} days"], ["Decision log rows", str(len(decisions))],
    ])
    L += ["", "Sleep, EA and the HRV floor start Oct 30-31 (morning checks); older days have no value, not a zero.", ""]

    # 1. volume and consistency
    L += ["## 1. Volume and consistency", ""]
    full = [w for w in weeks if w["days"] == 7]
    last26 = full[-26:]
    L += table(["Week", "Plan", "km", "Runs", "Longest", "Hard", "Lift days", "Sleep h", "HRV", "RHR", "EA", "TRIMP", "Monotony"],
               [[w["week"], w["label"], w["km"], w["runs"], f(w["longest"], 1), w["hard"], w["lift_days"], w["sleep_h"],
                 w["hrv"], w["rhr"], w["ea"], w["trimp"], w["monotony"]] for w in last26])
    kms = [w["km"] for w in last26]
    if len(kms) >= 4:
        cv = st.pstdev(kms) / st.mean(kms) if st.mean(kms) else None
        L += ["", f"- Full weeks: {len(last26)}; mean {st.mean(kms):.1f} km, median {st.median(kms):.1f}, "
                  f"range {min(kms):.1f}-{max(kms):.1f}, week-to-week variation (CV) {f(100 * cv if cv else None, 0)} %."]
        L.append(f"- Weeks with a 16 km+ run: {sum(1 for w in last26 if w['longest'] >= 16)} of {len(last26)}; "
                 f"weeks under 30 km: {sum(1 for w in last26 if w['km'] < 30)}.")
        runs_pw = [w["runs"] for w in last26]
        L.append(f"- Runs per week: mean {st.mean(runs_pw):.1f} (min {min(runs_pw)}, max {max(runs_pw)}).")
        if cv and cv > 0.2:
            flags.append(f"Weekly km swings a lot (CV {100 * cv:.0f} %): consistency, not peaks, drives aerobic gains.")
    L.append("")

    # 2. long runs
    L += ["## 2. Long runs (15 km+)", ""]
    longs = [r for r in runs if (n(r["km"]) or 0) >= 15]
    L += table(["Date", "km", "Pace", "Avg HR", "Aerobic TE", "Merged"],
               [[r["date"], r["km"], r.get("pace") or pace(n(r.get("pace_s"))), r["avg_hr"], r["aerobic_te"],
                 "yes" if r.get("merged") == "1" else ""] for r in longs[-20:]])
    hard_longs = [r for r in longs[-8:] if (n(r["aerobic_te"]) or 0) >= 4.0]
    L += ["", f"- Last {min(8, len(longs))} long runs with aerobic TE 4.0+: {len(hard_longs)} "
              "(a long run at TE 4+ is a quality session, not an easy one)."]
    if len(hard_longs) >= 3:
        flags.append(f"{len(hard_longs)} of the last {min(8, len(longs))} long runs reached aerobic TE 4.0+: long runs are run as quality sessions.")
    L.append("")

    # 3. intensity
    L += ["## 3. Intensity", ""]
    recent = [r for r in runs if r["date"] >= (today - timedelta(days=84)).isoformat()]
    kinds = Counter((r.get("kind") or "easy") for r in recent)
    easy = [r for r in recent if not r.get("kind")]
    easy_hr = [n(r["avg_hr"]) for r in easy if n(r["avg_hr"])]
    L.append(f"- Last 12 weeks: {len(recent)} runs: {kinds.get('easy', 0)} easy, {kinds.get('hard', 0)} hard "
             f"(aerobic TE 3.5+ or anaerobic 2.0+), {kinds.get('long', 0)} long.")
    if easy_hr:
        z2 = sum(1 for h in easy_hr if h > Z1_TOP)
        L.append(f"- Easy runs: median avg HR {st.median(easy_hr):.0f} bpm; {z2} of {len(easy_hr)} averaged Zone 2 (162+); "
                 f"{sum(1 for h in easy_hr if h >= 170)} averaged 170+.")
        if pct(z2, len(easy_hr)) and pct(z2, len(easy_hr)) > 40:
            flags.append(f"{pct(z2, len(easy_hr)):.0f} % of easy runs averaged Zone 2+: easy days drifting up (P2).")
    te_easy = [n(r["aerobic_te"]) for r in easy if n(r["aerobic_te"]) is not None]
    if te_easy:
        L.append(f"- Easy-run aerobic TE: median {st.median(te_easy):.1f}; {sum(1 for t in te_easy if t >= 3.0)} of {len(te_easy)} at 3.0+.")
    L.append("")

    # 4. aerobic efficiency
    L += ["## 4. Aerobic efficiency (speed per heartbeat on steady easy runs)", ""]
    by_month = defaultdict(list)
    for r in runs:
        hr, ps, km = n(r["avg_hr"]), n(r.get("pace_s")), n(r["km"])
        if hr and ps and km and km >= 5 and 130 <= hr <= 165 and not r.get("kind"):
            by_month[r["date"][:7]].append((1000 / ps * 60) / hr)       # m/min per beat
    rows = []
    for mth in sorted(by_month):
        ef = st.median(by_month[mth])
        rows.append([mth, len(by_month[mth]), f(ef, 3), pace(60000 / (ef * 150)) if ef else "–"])   # s/km at 150 bpm
    L += table(["Month", "Easy runs", "Median m/min per beat", "Equivalent pace at 150 bpm /km"], rows)
    L += ["", "Pace at 150 bpm = 1000 / (efficiency x 150) min/km. The tracker's monthly Pace@150 (section 5) uses its own",
          "selection; compare trends, not single numbers. Heat and hills lower efficiency.", ""]

    # 5. monthly trends (tracker)
    L += ["## 5. Monthly trends (brain_athlete_history.md; weight = fasted monthly average)", ""]
    if hist["monthly"]:
        head = list(hist["monthly"][0].keys())
        L += table(head, [[m.get(h, "") for h in head] for m in hist["monthly"]])
    L.append("")

    # 6. recovery
    L += ["## 6. Recovery: HRV, resting HR, sleep", ""]
    hrv_m, rhr_m = defaultdict(list), defaultdict(list)
    for r in done:
        if n(r.get("hrv")) is not None:
            hrv_m[r["date"][:7]].append(n(r["hrv"]))
        if n(r.get("rhr")) is not None:
            rhr_m[r["date"][:7]].append(n(r["rhr"]))
    L += table(["Month", "HRV nights", "HRV mean", "HRV min", "RHR mean"],
               [[m, len(hrv_m[m]), f(mean(hrv_m[m]), 0), f(min(hrv_m[m]), 0), f(mean(rhr_m.get(m, [])), 1)]
                for m in sorted(hrv_m)])
    with_floor = [r for r in done if n(r.get("hrv")) is not None and n(r.get("hrv_floor")) is not None]
    below = [r for r in with_floor if n(r["hrv"]) < n(r["hrv_floor"])]
    L.append("")
    L.append(f"- Nights below Garmin's balanced-low (floor known): {len(below)} of {len(with_floor)} "
             f"({', '.join(r['date'] + ' ' + f(n(r['hrv']), 0) for r in below) or 'none'}).")
    sleep = [n(r["sleep_h"]) for r in done if n(r.get("sleep_h")) is not None]
    if sleep:
        short6 = sum(1 for s in sleep if s < 6)
        short7 = sum(1 for s in sleep if s < 7)
        L.append(f"- Sleep ({len(sleep)} nights): mean {st.mean(sleep):.2f} h, median {st.median(sleep):.2f} h; "
                 f"under 6 h on {short6} nights ({pct(short6, len(sleep)):.0f} %), under 7 h on {short7} ({pct(short7, len(sleep)):.0f} %).")
        wd = defaultdict(list)
        for r in done:
            if n(r.get("sleep_h")) is not None:
                wd[date.fromisoformat(r["date"]).weekday()].append(n(r["sleep_h"]))
        L.append("- Sleep by morning: " + ", ".join(f"{WEEKDAYS[k]} {st.mean(v):.1f} h (n={len(v)})" for k, v in sorted(wd.items())) + ".")
        if st.mean(sleep) < 7:
            flags.append(f"Sleep averages {st.mean(sleep):.1f} h ({pct(short7, len(sleep)):.0f} % of nights under 7 h, "
                         f"{pct(short6, len(sleep)):.0f} % under 6 h): the cheapest recovery lever left.")
    L.append("")

    # 7. load
    L += ["## 7. Load: ACWR and monotony", ""]
    if acwr:
        vals = [v for _, v in acwr]
        bands = Counter("< 0.8" if v < 0.8 else "0.8-1.3" if v <= 1.3 else "1.3-1.5" if v <= 1.5 else "> 1.5" for v in vals)
        L.append(f"- Mechanical ACWR on {len(vals)} days ({acwr[0][0]} to {acwr[-1][0]}): "
                 + ", ".join(f"{b} {bands.get(b, 0)} days" for b in ("< 0.8", "0.8-1.3", "1.3-1.5", "> 1.5"))
                 + f"; latest {vals[-1]:.2f}.")
        spikes = [(d, v) for d, v in acwr if v > 1.3]
        if spikes:
            L.append("- Days above 1.3: " + ", ".join(f"{d} {v:.2f}" for d, v in spikes[-10:]) + ".")
    mono = [(w["week"], n(w["monotony"])) for w in last26 if n(w["monotony"]) is not None]
    if mono:
        L.append(f"- Training monotony (mean / SD of daily TRIMP, Foster; > 2.0 = little day-to-day variation): "
                 f"median {st.median([m for _, m in mono]):.2f}; weeks above 2.0: {sum(1 for _, m in mono if m > 2.0)}.")
    L.append("")

    # 8. fuel
    L += ["## 8. Fuel: energy availability and macros", ""]
    ea_days = [r for r in done if n(r.get("ea")) is not None]
    if ea_days:
        eas = [n(r["ea"]) for r in ea_days]
        lo, mid, hi = sum(1 for e in eas if e < 30), sum(1 for e in eas if 30 <= e < 45), sum(1 for e in eas if e >= 45)
        L.append(f"- EA on {len(eas)} days (kcal per kg lean mass {LEAN_MASS_KG} after exercise): mean {st.mean(eas):.1f}, "
                 f"median {st.median(eas):.1f}; < 30: {lo} days, 30-45: {mid}, >= 45: {hi}.")
        run_ea = [n(r["ea"]) for r in ea_days if (n(r["km"]) or 0) > 0]
        rest_ea = [n(r["ea"]) for r in ea_days if (n(r["km"]) or 0) == 0]
        L.append(f"- EA on run days {f(mean(run_ea))} (n={len(run_ea)}) vs no-run days {f(mean(rest_ea))} (n={len(rest_ea)}).")
        if pct(lo, len(eas)) and pct(lo, len(eas)) >= 15:
            flags.append(f"EA under 30 on {lo} of {len(eas)} days ({pct(lo, len(eas)):.0f} %): low energy availability (P30).")
        elif st.mean(eas) < 40:
            flags.append(f"EA averages {st.mean(eas):.1f}: below the 45 target most days (P30).")
    if nutrition:
        prot = [r["protein"] for r in nutrition if r["protein"]]
        carbs = [r["carbs"] for r in nutrition if r["carbs"]]
        kcal = [r["kcal"] for r in nutrition if r["kcal"]]
        L.append(f"- Nutrition log ({len(nutrition)} days, {nutrition[0]['date']} to {nutrition[-1]['date']}): kcal mean {f(mean(kcal), 0)}, "
                 f"protein {f(mean(prot), 0)} g, carbs {f(mean(carbs), 0)} g (Rule 7: protein 110-130 g; carbs 5-7 g/kg on run days).")
    L.append("")

    # 9. strength
    L += ["## 9. Strength", ""]
    lift_wk = [w["lift_days"] for w in last26[-12:]]
    if lift_wk:
        L.append(f"- Lifting days per week (last {len(lift_wk)} weeks, Garmin strength activities): mean {st.mean(lift_wk):.1f}, "
                 f"max {max(lift_wk)}; weeks with 5+: {sum(1 for x in lift_wk if x >= 5)}.")
        if st.mean(lift_wk) >= 4.5:
            flags.append(f"Lifting {st.mean(lift_wk):.1f} days a week on average: little room for a day off the body (Rule 10, P21).")
    by_lift = defaultdict(list)
    for r in lifts:
        by_lift[r["lift"]].append((r["month"], n(r["e1rm_kg"])))
    L += table(["Lift", "First month", "Latest month", "Best"],
               [[k, f"{v[0][0]} {f(v[0][1], 0)}", f"{v[-1][0]} {f(v[-1][1], 0)}", f(max(x for _, x in v if x is not None), 0)]
                for k, v in by_lift.items() if v])
    prm = Counter(d[:7] for d in pr_dates)
    L += ["", "- Rows marked **PR** in brain_strength_records.md by month: " + (", ".join(f"{k} {v}" for k, v in sorted(prm.items())) or "none") + ".", ""]

    # 10. behaviour
    L += ["## 10. Behaviour: plan adherence, timing, decisions", ""]
    if adh:
        oc = Counter(a["outcome"] for a in adh)
        L.append(f"- Plan vs Garmin on {len(adh)} days whose plan was not overwritten ({adh[0]['date']} to {adh[-1]['date']}): "
                 + ", ".join(f"{k} {v}" for k, v in oc.most_common()) + ".")
        ext = [a for a in adh if a["outcome"] == "extended"]
        if ext:
            ratios = [a["actual_km"] / a["planned_km"] for a in ext if a["planned_km"]]
            L.append(f"- Extended runs: median {st.median(ratios):.2f} x planned ({', '.join(a['date'] + ' ' + str(a['planned_km']) + '->' + str(a['actual_km']) for a in ext[:8])}).")
        L.append("- Note: completed days in later week plans hold the actual km, so their plan is lost; this table "
                 "covers only days still holding the plan (build_plan T15 keeps both).")
    starts = [r["start"] for r in runs if r.get("start")]
    if starts:
        hours = Counter(int(s[:2]) for s in starts)
        late = sum(v for h, v in hours.items() if h >= 19)
        early = sum(v for h, v in hours.items() if h < 9)
        L.append(f"- Run start times ({len(starts)} runs with a start time): before 9:00 {early}, 9:00-16:59 "
                 f"{sum(v for h, v in hours.items() if 9 <= h < 17)}, 17:00-18:59 {sum(v for h, v in hours.items() if 17 <= h < 19)}, 19:00+ {late}.")
    wdays = Counter(date.fromisoformat(r["date"]).weekday() for r in runs if r["date"] >= (today - timedelta(days=84)).isoformat())
    L.append("- Runs by weekday, last 12 weeks: " + ", ".join(f"{WEEKDAYS[i]} {wdays.get(i, 0)}" for i in range(7)) + ".")
    labels = [str(e.get("label", "")) + " " + str(e.get("session", "")) for e in plans.values()]
    n_rule13 = sum(1 for s in labels if "Rule 13" in s)
    n_dropped = sum(1 for s in labels if re.search(r"dropp", s, re.I))
    n_extra = sum(1 for s in labels if re.search(r"\bextra\b|off-plan", s, re.I))   # outside the f-string: Python < 3.12
    L.append(f"- Week-plan days mentioning Rule 13 (off-plan changes): {n_rule13}; 'dropped': {n_dropped}; "
             f"'extra' or 'off-plan': {n_extra}.")
    if decisions:
        L.append(f"- Decision log: {len(decisions)} consults/overrides; outcomes filled in: "
                 f"{sum(1 for d in decisions if d['outcome'])} of {len(decisions)}.")
    L.append("")

    # 11. exploratory correlations
    L += ["## 11. Exploratory correlations (small samples: hypotheses, not findings)", ""]
    nxt = {r["date"]: r for r in done}
    pairs = defaultdict(lambda: ([], []))

    def add(name, x, y):
        pairs[name][0].append(x)
        pairs[name][1].append(y)
    for r in done:
        d = date.fromisoformat(r["date"])
        tomorrow = nxt.get((d + timedelta(days=1)).isoformat())
        if not tomorrow:
            continue
        add("Sleep h (night before D+1) -> HRV on D+1", n(tomorrow.get("sleep_h")), n(tomorrow.get("hrv")))
        add("EA on D -> HRV on D+1", n(r.get("ea")), n(tomorrow.get("hrv")))
        add("TRIMP on D -> HRV on D+1", n(r.get("trimp")), n(tomorrow.get("hrv")))
        add("Lifted on D (1/0) -> HRV on D+1", n(r.get("lifted")), n(tomorrow.get("hrv")))
        add("Run km on D -> RHR on D+1", n(r.get("km")), n(tomorrow.get("rhr")))
    rows = []
    for name, (xs, ys) in pairs.items():
        rs, cnt = spearman(xs, ys)
        rows.append([name, cnt, f(rs, 2) if cnt >= MIN_CORR_N and rs is not None else "too few"])
    L += table(["Pair", "n", "Spearman r"], rows)
    L += ["", "|r| < 0.3 is weak. With n under ~30 a single odd day can flip the sign; repeat after T15's ledger grows.", ""]

    # 12. biomechanics
    L += ["## 12. Biomechanics and run quality (core_run_analysis reports)", ""]
    bm = defaultdict(list)
    for r in biomech:
        if not r["treadmill"]:
            bm[r["date"][:7]].append(r)
    L += table(["Month", "Outdoor runs", "Cadence", "GCT ms", "Vertical ratio %", "Decoupling %", "Heat index C"],
               [[m, len(v), f(median([x["cadence"] for x in v]), 0), f(median([x["gct_ms"] for x in v]), 0),
                 f(median([x["vert_ratio"] for x in v]), 1), f(median([x["decoupling_pct"] for x in v]), 1),
                 f(median([x["heat_index"] for x in v]), 1)] for m, v in sorted(bm.items())])
    L.append("")

    # assemble
    fit = []
    for key, label in (("vo2max", "VO2max"), ("lifetime_longest_km", "Lifetime longest"), ("longest_30d_km", "Longest, last 30 days"),
                       ("pace150_out", "Pace at 150 bpm outdoor"), ("predict_marathon", "Garmin marathon prediction")):
        if key in fitness:
            fit.append(f"{label} {fitness[key]['value']}{(' ' + fitness[key]['unit']) if fitness[key]['unit'] and fitness[key]['unit'] not in ('h:mm:ss',) else ''} ({fitness[key]['date']})")
    head = [f"# Telemetry and history rundown ({today.isoformat()})", "",
            "*Generated by `scripts/telemetry_review.py` from repo files only. Same input, same output, any model.*", "",
            "**Snapshot:** " + "; ".join(fit) + ".", "", "## Auto-flags", ""]
    head += [f"- {x}" for x in flags] or ["- none"]
    head.append("")
    return {"markdown": "\n".join(head + L) + "\n", "weekly": weeks, "biomech": biomech, "adherence": adh, "flags": flags}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in PHT)")
    ap.add_argument("--out", help="output folder (default reviews/<YYYY-MM>-deep/packet)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else today_pht()
    out = Path(args.out) if args.out else ROOT / "reviews" / f"{today:%Y-%m}-deep" / "packet"
    out.mkdir(parents=True, exist_ok=True)
    res = build(ROOT, today)
    (out / "rundown.md").write_text(res["markdown"], encoding="utf-8")
    write_csv(out / "weekly.csv", res["weekly"], ["week", "label", "days", "km", "runs", "longest", "hard", "lift_days",
                                                   "sleep_h", "hrv", "rhr", "ea", "trimp", "monotony"])
    write_csv(out / "run_biomech.csv", res["biomech"], ["date", "activity_id", "name", "treadmill", "km", "pace_s", "avg_hr",
                                                         "aerobic_te", "decoupling_pct", "hr_drift", "trimp", "epoc",
                                                         "cadence", "gct_ms", "vert_ratio", "heat_index"])
    write_csv(out / "adherence.csv", res["adherence"], ["date", "weekday", "planned_km", "actual_km", "outcome", "plan_file"])
    print(f"[OK] {len(res['flags'])} flags -> {os.path.relpath(out, ROOT) if str(out).startswith(str(ROOT)) else out}")
    for x in res["flags"]:
        print(f"  - {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
