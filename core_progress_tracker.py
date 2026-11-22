"""
core_progress_tracker.py — milestone ledger, current fitness and plateau check
==============================================================================
Reads the athlete's full Garmin history and writes brain_athlete_history.md, which the coach
reads at every planning session. Garmin data is the source, never memory, so a half
marathon is never called a "milestone" when he has run 19 of them.

Sections written:
  1. Milestone ledger: runs per distance band, longest runs, Garmin PRs, race predictions.
  2. Capacity vs. readiness: lifetime longest next to the longest of the last 30 days.
  3. Current fitness (last 28 days): pace at 150 bpm (outdoor and treadmill separately),
     long-run pace, weekly km, outdoor cadence, VO2max.
  4. Monthly trends: weekly km, longest run, pace at 150 bpm, VO2max, HRV, resting HR,
     weight, outdoor cadence, 5K time trials, main-lift e1RM.
  5. Plateau check: last 6 weeks vs. the 6 before for each trend -> improving / plateau /
     declining. A plateau is flagged only once it has held for 4+ weeks (two checks in a
     row), tracked in brain_progress_state.json. Flagged plateaus get the matching
     breakthrough from the playbook (knowledge_base/PRINCIPLES.md).

"Pace at 150 bpm" = efficiency (metres per heartbeat) of easy runs (avg HR 130-165,
>= 5 km) x 150 bpm. Higher efficiency = faster at the same heart rate. Treadmill cadence
is ignored (the Forerunner 165 misreads it); heat makes outdoor efficiency noisier.

Usage:
    python core_progress_tracker.py [--no-strength] [--no-write]
"""

import argparse
import json
import os
import statistics
from collections import defaultdict
from datetime import date, timedelta

from core_common import is_run, merge_notes, merge_split_runs, run_km, today_pht
from garmin_auth import get_client

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MD = os.path.join(HERE, "brain_athlete_history.md")
STATE = os.path.join(HERE, "brain_progress_state.json")
HISTORY_START = date(2025, 8, 1)      # the athlete's Garmin running history starts Aug 21, 2025

BANDS = [(10, "10 km+"), (15, "15 km+"), (21.0975, "Half marathon+ (21.1 km)"), (25, "25 km+"),
         (30, "30 km+"), (32, "32 km+"), (35, "35 km+"), (42.195, "Marathon+")]
PR_TYPES = {1: "1 km", 2: "1 mile", 3: "5 km", 4: "10 km", 5: "Half marathon", 6: "Marathon",
            7: "Longest run"}
MAIN_LIFTS = {"BARBELL_BENCH_PRESS": "Bench", "LAT_PULLDOWN": "Lat pulldown",
              "SEATED_CABLE_ROW": "Seated row", "LEG_PRESS": "Leg press",
              "BARBELL_BACK_SQUAT": "Squat", "BARBELL_DEADLIFT": "Deadlift"}
REF_HR = 150

# Plateau thresholds: change smaller than this (6 weeks vs the 6 before) = plateau.
# sign: +1 when higher is better, -1 when lower is better.
METRICS = {
    "weekly_km":   ("Weekly km", 0.05, +1, "rel"),
    "long_run":    ("Longest run per week", 0.05, +1, "rel"),
    "ef_outdoor":  ("Pace at 150 bpm (outdoor)", 0.01, +1, "rel"),
    "ef_treadmill": ("Pace at 150 bpm (treadmill)", 0.01, +1, "rel"),
    "vo2max":      ("VO2max", 0.5, +1, "abs"),
    "hrv":         ("HRV (nightly avg)", 0.03, +1, "rel"),
    "rhr":         ("Resting HR", 1.0, -1, "abs"),
    "cadence":     ("Outdoor cadence", 1.0, +1, "abs"),
}
PLAYBOOK = {
    "weekly_km": "Volume ceiling: +5-10% per week held 2-3 weeks, recovery week every 3rd-4th (P5, P6).",
    "long_run": "Durability: extend the long run within the 10% single-run cap; fast-finish/MP segments (Rule 5, P8).",
    "ef_outdoor": "Aerobic base still adapting: more easy volume and patience; if volume is rising and this is "
                  "still flat after 8+ weeks, add a threshold block (P4, P13).",
    "ef_treadmill": "Same as outdoor: more easy volume first, then a threshold block (P4, P13).",
    "vo2max": "No high-end stimulus: a 4-6 week threshold/VO2max block plus strides (P10-P13).",
    "hrv": "Recovery- or fuel-limited: lighter week, energy-availability check, sleep (P30, P31).",
    "rhr": "Rising resting HR: check fatigue, illness, heat and fuelling before adding load (P31).",
    "cadence": "Cadence flat is fine on its own; only act if economy is also flat (strides, hill sprints: P10, P11).",
    "strength": "Lift stalled 4+ weeks: change the rep scheme or the exercise variation (Rule 10).",
}


def fmt_pace(sec_per_km):
    if not sec_per_km:
        return "--"
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


def ef(act):
    """Metres per heartbeat for an easy run, else None."""
    hr, km, dur = act.get("averageHR"), run_km(act), act.get("duration") or 0
    if not hr or km < 5 or dur <= 0 or not (130 <= hr <= 165):
        return None
    return (km * 1000 / (dur / 60)) / hr


def ef_to_pace(e):
    return 1000 / (e * REF_HR) * 60 if e else None   # sec per km at 150 bpm


def is_time_trial(act):
    name = (act.get("activityName") or "").lower()
    km = run_km(act)
    if "time trial" in name or " tt" in f" {name}" or "5k tt" in name:
        return True
    return 4.9 <= km <= 5.5 and (act.get("aerobicTrainingEffect") or 0) >= 4.0


def week_of(d):
    return d - timedelta(days=d.weekday())


# Daily metrics that come from separate Garmin endpoints. If one of them cannot be read, the
# history file keeps the last known monthly values and says so, instead of silently writing "--".
GAP_COLUMNS = {"vo2max": "VO2max", "hrv": "HRV", "rhr": "RHR", "weight": "Weight"}
CHUNK_DAYS = 180


def current_week_is_complete(today, sunday_session_done):
    """Weeks run Monday-Sunday. The current week counts as complete only on Sunday once Sunday's
    session is done: the Sunday morning check runs at 7 am, before a Sunday long run."""
    return today.weekday() == 6 and bool(sunday_session_done)


def sunday_session_done(today, runs, plan=None):
    """Is today's running done? True when a run is already logged today, or the week plan has no
    run today (or marks the day completed). Without a plan, only a logged run counts."""
    if any(a["_date"] == today for a in runs):
        return True
    entry = ((plan or {}).get("days") or {}).get(today.isoformat())
    if entry is None:
        return False
    return bool(entry.get("completed")) or float(entry.get("run_km") or 0) == 0


def load_week_plan():
    try:
        from brain_current_week_plan import plan
        return plan
    except Exception as e:   # the tracker still runs without a plan
        print(f"[WARN] Could not load brain_current_week_plan.py: {e}")
        return None


def fetch_daily_metrics(c, start, today):
    """Daily VO2max, HRV, resting HR and weight since `start`.

    Returns (daily, gaps): daily = {metric: {iso_date: value}}; gaps = {metric: reason} for
    every metric Garmin gave nothing for (an error, or an empty answer)."""
    daily, errors = {}, {}

    def put(key, d, v):
        if d and v:
            daily.setdefault(key, {})[d] = v

    def attempt(key, fn):
        try:
            fn()
        except Exception as e:   # one failed endpoint must not take the others down
            errors[key] = f"{type(e).__name__}: {e}"[:160]

    def vo2(s, e):
        for m in c.get_max_metrics_range(s.isoformat(), e.isoformat()) or []:
            g = (m or {}).get("generic") or {}
            put("vo2max", g.get("calendarDate"), g.get("vo2MaxPreciseValue"))

    def hrv(s, e):
        for h in (c.get_hrv_data_range(s.isoformat(), e.isoformat()) or {}).get("hrvSummaries") or []:
            put("hrv", h.get("calendarDate"), h.get("lastNightAvg"))

    def rhr(s, e):
        for r in c.get_rhr_daily(s.isoformat(), e.isoformat()) or []:
            put("rhr", r.get("calendarDate"), r.get("value"))

    def weight():
        for w in (c.get_weigh_ins(start.isoformat(), today.isoformat()) or {}).get("dailyWeightSummaries") or []:
            lw = (w.get("latestWeight") or {}).get("weight")
            put("weight", w.get("summaryDate"), lw / 1000 if lw else None)

    for offset in range(0, (today - start).days + 1, CHUNK_DAYS):   # 6-month chunks
        s = start + timedelta(days=offset)
        e = min(today, s + timedelta(days=CHUNK_DAYS - 1))
        attempt("vo2max", lambda: vo2(s, e))
        attempt("hrv", lambda: hrv(s, e))
        attempt("rhr", lambda: rhr(s, e))
    attempt("weight", weight)
    if not daily.get("vo2max"):   # range endpoint gave nothing: at least read the current value
        def vo2_today():
            for m in c.get_max_metrics(today.isoformat()) or []:
                g = (m or {}).get("generic") or {}
                put("vo2max", g.get("calendarDate"), g.get("vo2MaxPreciseValue"))
        attempt("vo2max", vo2_today)

    gaps = {}
    for key in GAP_COLUMNS:
        if not daily.get(key):
            gaps[key] = errors.get(key, "Garmin returned no data")
        elif key in errors:
            gaps[key] = f"partly missing ({errors[key]})"
    return daily, gaps


def previous_history(path):
    """Last-known values from the existing history file, used to fill a data gap.

    Returns {"generated": 'YYYY-MM-DD' or '', "monthly": {month: {column: cell}}, "vo2max_line": str}."""
    out = {"generated": "", "monthly": {}, "vo2max_line": ""}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines):
        if line.startswith("# Athlete History (generated "):
            out["generated"] = line[len("# Athlete History (generated "):][:10]
        elif line.startswith("- **VO2max:**"):
            out["vo2max_line"] = line.split(" (last known", 1)[0]
        elif line.startswith("| Month |"):
            cols = [x.strip() for x in line.strip().strip("|").split("|")]
            for row in lines[i + 2:]:
                if not row.startswith("|"):
                    break
                cells = [x.strip() for x in row.strip().strip("|").split("|")]
                out["monthly"][cells[0]] = dict(zip(cols, cells))
    return out


def fill_gap(cell, month, key, gaps, prev):
    """A monthly cell: the fresh value, or the last known one when this metric has a gap today."""
    if cell != "--" or key not in gaps:
        return cell
    return (prev.get("monthly", {}).get(month) or {}).get(GAP_COLUMNS[key]) or "--"


def main():
    ap = argparse.ArgumentParser(description="Progress tracker")
    ap.add_argument("--no-strength", action="store_true", help="skip lift e1RM (faster)")
    ap.add_argument("--no-write", action="store_true", help="print only")
    args = ap.parse_args()

    today = today_pht()
    c = get_client()
    acts = c.get_activities_by_date(HISTORY_START.isoformat(), today.isoformat()) or []
    runs = sorted([a for a in acts if is_run(a)], key=lambda a: a["startTimeLocal"])
    for a in runs:
        a["_date"] = date.fromisoformat(a["startTimeLocal"][:10])
        a["_km"] = run_km(a)
        a["_outdoor"] = a.get("activityType", {}).get("typeKey") != "treadmill_running"
    # Distance records count a split run (e.g. outdoor + treadmill parts) as one run;
    # pace and efficiency trends keep the parts separate (outdoor vs treadmill).
    sessions = sorted([a for a in merge_split_runs(acts) if is_run(a)], key=lambda a: a["startTimeLocal"])
    for a in sessions:
        a["_date"] = date.fromisoformat(a["startTimeLocal"][:10])
        a["_km"] = run_km(a)
        a["_outdoor"] = a.get("activityType", {}).get("typeKey") != "treadmill_running"
    L = []

    # ── 1. Ledger ──────────────────────────────────────────────────────────
    L.append(f"# Athlete History (generated {today} by core_progress_tracker.py: do not edit by hand)\n")
    L.append("Read this before calling any run a milestone, setting a pace, or judging progress. "
             "Source: Garmin, since " + (runs[0]["startTimeLocal"][:10] if runs else "n/a") + ".\n")
    L.append("## 1. Milestone ledger\n")
    L.append(f"{len(sessions)} runs, {sum(a['_km'] for a in sessions):,.0f} km in total "
             f"(same-day runs <= 90 min apart count as one run).\n")
    L.append("| Distance | Runs | First | Most recent | Longest |")
    L.append("|---|---|---|---|---|")
    for km, label in BANDS:
        band = [a for a in sessions if a["_km"] >= km - 0.05]
        if band:
            longest = max(band, key=lambda a: a["_km"])
            L.append(f"| {label} | {len(band)} | {band[0]['_date']} | {band[-1]['_date']} | "
                     f"{longest['_km']:.2f} km ({longest['_date']}) |")
        else:
            L.append(f"| {label} | 0 | never | never | not yet: this would be a real milestone |")
    L.append("\n**Longest runs:**\n")
    L.append("| Date | km | Time | Pace | Avg HR | Where |")
    L.append("|---|---|---|---|---|---|")
    for a in sorted(sessions, key=lambda a: -a["_km"])[:10]:
        d = a.get("duration") or 0
        hr = a.get('averageHR')
        where = "outdoor + treadmill" if a.get("_mixed") else "outdoor" if a['_outdoor'] else "treadmill"
        L.append(f"| {a['_date']} | {a['_km']:.2f} | {int(d // 3600)}:{int(d % 3600 // 60):02d} | "
                 f"{fmt_pace(d / a['_km'])} | {round(hr) if hr else '--'} | {where} |")
    notes = merge_notes(sessions)
    if notes:
        L.append("\n**Merged split runs (check these):**\n")
        L += [f"- {n}" for n in notes]
    try:
        prs = c.get_personal_record() or []
        rows = []
        for p in prs:
            label = PR_TYPES.get(p.get("typeId"))
            if not label:
                continue
            v = p.get("value") or 0
            when = (p.get("actStartDateTimeInGMTFormatted") or "")[:10]
            val = f"{v / 1000:.2f} km" if p["typeId"] == 7 else \
                f"{int(v // 3600)}:{int(v % 3600 // 60):02d}:{int(v % 60):02d}" if v >= 3600 else \
                f"{int(v // 60)}:{int(v % 60):02d}"
            rows.append((p["typeId"], f"| {label} | {val} | {when} |"))
        if rows:
            L.append("\n**Garmin personal records:**\n")
            L.append("| Record | Value | Date |")
            L.append("|---|---|---|")
            L += [r for _, r in sorted(rows)]
    except Exception as e:
        L.append(f"\n(Garmin PRs unavailable: {e})")
    try:
        rp = c.get_race_predictions() or {}
        def t(s):
            return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(s % 60):02d}" if s else "--"
        L.append(f"\n**Garmin race predictions ({rp.get('calendarDate', today)}):** 5K {t(rp.get('time5K'))}, "
                 f"10K {t(rp.get('time10K'))}, half {t(rp.get('timeHalfMarathon'))}, "
                 f"marathon {t(rp.get('timeMarathon'))}.")
    except Exception as e:
        L.append(f"\n(Garmin race predictions unavailable: {e})")

    # ── 2. Capacity vs readiness ───────────────────────────────────────────
    life = max(sessions, key=lambda a: a["_km"]) if sessions else None
    recent = [a for a in sessions if a["_date"] >= today - timedelta(days=30)]
    r30 = max(recent, key=lambda a: a["_km"]) if recent else None
    L.append("\n## 2. Capacity vs. current readiness\n")
    if life:
        L.append(f"- **Lifetime longest:** {life['_km']:.2f} km ({life['_date']}). This is capacity: "
                 "distances up to here are not milestones.")
    if r30:
        L.append(f"- **Longest in the last 30 days:** {r30['_km']:.2f} km ({r30['_date']}). This is readiness: "
                 f"the single-run 10% cap today is **{r30['_km'] * 1.1:.1f} km** (P8).")
    L.append("- Every long-run decision states both numbers.")

    # ── Weekly series (for trends and plateau check) ───────────────────────
    weekly = defaultdict(lambda: defaultdict(list))
    for a in runs:
        w = week_of(a["_date"])
        weekly[w]["km"].append(a["_km"])
        e = ef(a)
        if e:
            weekly[w]["ef_outdoor" if a["_outdoor"] else "ef_treadmill"].append(e)
        cad = a.get("averageRunningCadenceInStepsPerMinute")
        if cad and a["_outdoor"]:
            weekly[w]["cadence"].append(cad)
        if is_time_trial(a):
            weekly[w]["tt"].append((a.get("duration") or 0) / a["_km"] * 5)
    for a in sessions:   # longest run per week counts a split run as one
        weekly[week_of(a["_date"])]["long"].append(a["_km"])
    prev = previous_history(OUT_MD)   # read before this run overwrites the file
    daily, gaps = fetch_daily_metrics(c, HISTORY_START, today)
    for key, reason in gaps.items():
        print(f"[DATA GAP] {GAP_COLUMNS[key]}: {reason}")
    for key, series in daily.items():
        for d, v in series.items():
            weekly[week_of(date.fromisoformat(d))][key].append(v)

    lifts = {}
    if not args.no_strength:
        try:
            from core_strength_progression import epley, fetch_sessions
            hist = fetch_sessions(c, (today - HISTORY_START).days)
            for name, label in MAIN_LIFTS.items():
                for sess in hist.get(name, []):
                    best = max((epley(w, r) for w, r in sess["sets"] if w and r <= 10), default=0)
                    if best:
                        lifts.setdefault(label, {})[sess["date"]] = best
        except Exception as e:
            L.append(f"\n(Lift history unavailable: {e})")

    def wk_value(w, key):
        v = weekly[w].get(key) or []
        if not v:
            return None
        if key == "km":
            return sum(v)
        return statistics.mean(v)

    # ── 3. Current fitness ─────────────────────────────────────────────────
    # Weeks run Monday-Sunday; the current week counts as complete only once Sunday's session is done.
    week_done = current_week_is_complete(today, today.weekday() == 6 and
                                         sunday_session_done(today, runs, load_week_plan()))
    next_week = week_of(today) + timedelta(weeks=1) if week_done else week_of(today)
    last4 = [next_week - timedelta(weeks=i) for i in range(4)]
    last12 = [next_week - timedelta(weeks=i) for i in range(12)]
    def window_mean(key, weeks_):
        vals = [x for w in weeks_ for x in (weekly[w].get(key) or [])]
        return statistics.mean(vals) if vals else None
    L.append("\n## 3. Current fitness (last 4 weeks)\n")
    L.append(f"- **Pace at {REF_HR} bpm, outdoor:** {fmt_pace(ef_to_pace(window_mean('ef_outdoor', last4)))}/km"
             f" | treadmill: {fmt_pace(ef_to_pace(window_mean('ef_treadmill', last4)))}/km "
             "(easy runs; use these for easy-pace targets).")
    longs = [a for a in sessions if a["_date"] >= today - timedelta(days=28) and a["_km"] >= 16]
    if longs:
        lp = statistics.mean((a.get("duration") or 0) / a["_km"] for a in longs)
        lh = statistics.mean(a.get("averageHR") or 0 for a in longs)
        L.append(f"- **Long-run pace (16 km+):** {fmt_pace(lp)}/km at avg HR {lh:.0f} ({len(longs)} runs).")
    wk4 = statistics.mean(sum(weekly[w].get("km") or [0]) for w in last4[1:]) if len(last4) > 1 else 0
    wk12 = statistics.mean(sum(weekly[w].get("km") or [0]) for w in last12[1:])
    L.append(f"- **Weekly km:** {wk4:.1f} (last 3 full weeks) | {wk12:.1f} (last 11 full weeks).")
    cad = window_mean("cadence", last4)
    L.append(f"- **Outdoor cadence:** {cad:.0f} spm." if cad else "- **Outdoor cadence:** no outdoor runs in 4 weeks.")
    if daily.get("vo2max"):
        d, v = max(daily["vo2max"].items())
        L.append(f"- **VO2max:** {v:.1f} ({d}).")
    elif prev["vo2max_line"]:
        L.append(f"{prev['vo2max_line']} (last known: Garmin gave no VO2max on {today}).")
    else:
        L.append(f"- **VO2max:** unavailable (Garmin gave no VO2max on {today}).")

    # ── 4. Monthly trends ──────────────────────────────────────────────────
    L.append("\n## 4. Monthly trends\n")
    L.append("| Month | km/week | Longest | Pace@150 out | Pace@150 tm | VO2max | HRV | RHR | Weight | Cadence | 5K TT |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    months = sorted({(a["_date"].year, a["_date"].month) for a in runs})
    for y, m in months[-13:]:
        mweeks = [w for w in weekly if w.year == y and w.month == m]
        mruns = [a for a in sessions if a["_date"].year == y and a["_date"].month == m]
        days_in = 31 if m == 12 else (date(y, m + 1, 1) - date(y, m, 1)).days
        if (y, m) == (today.year, today.month):
            days_in = today.day   # current month so far
        kmw = sum(a["_km"] for a in mruns) / days_in * 7
        def mm(key):
            vals = [x for w in mweeks for x in (weekly[w].get(key) or [])]
            return statistics.mean(vals) if vals else None
        tts = [x for w in mweeks for x in (weekly[w].get("tt") or [])]
        def f1(v, fmt="{:.1f}"):
            return fmt.format(v) if v else "--"
        mo = f"{y}-{m:02d}"
        def gap(key, cell):
            return fill_gap(cell, mo, key, gaps, prev)
        L.append(f"| {mo} | {kmw:.1f} | {max((a['_km'] for a in mruns), default=0):.1f} | "
                 f"{fmt_pace(ef_to_pace(mm('ef_outdoor')))} | {fmt_pace(ef_to_pace(mm('ef_treadmill')))} | "
                 f"{gap('vo2max', f1(mm('vo2max')))} | {gap('hrv', f1(mm('hrv'), '{:.0f}'))} | "
                 f"{gap('rhr', f1(mm('rhr'), '{:.0f}'))} | "
                 f"{gap('weight', f1(mm('weight')))} | {f1(mm('cadence'), '{:.0f}')} | "
                 f"{fmt_pace(min(tts) / 5) + '/km' if tts else '--'} |")
    if gaps:
        L.append(f"\n**Data gaps on {today}:** " + "; ".join(f"{GAP_COLUMNS[k]} ({r})" for k, r in gaps.items()) +
                 f". Empty cells in these columns show the last known values (history file of "
                 f"{prev['generated'] or 'n/a'}); their plateau rows below may read \"not enough data\".")
    if lifts:
        L.append("\n**Main lifts, best estimated 1RM per month (kg, sets of 10 reps or fewer):**\n")
        lmonths = sorted({d[:7] for s in lifts.values() for d in s})[-8:]
        L.append("| Lift | " + " | ".join(lmonths) + " |")
        L.append("|---|" + "---|" * len(lmonths))
        for label, s in lifts.items():
            cells = []
            for mo in lmonths:
                vals = [v for d, v in s.items() if d.startswith(mo)]
                cells.append(f"{max(vals):.0f}" if vals else "--")
            L.append(f"| {label} | " + " | ".join(cells) + " |")
    L.append("\n5K time trials are runs named \"time trial\"/\"TT\", or 4.9-5.5 km runs with aerobic TE 4.0+.")

    # ── 5. Plateau check ───────────────────────────────────────────────────
    L.append("\n## 5. Plateau check (last 6 weeks vs. the 6 before)\n")
    try:
        state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    except Exception:
        state = {}
    cur_week = next_week
    recent6 = [cur_week - timedelta(weeks=i) for i in range(1, 7)]
    prior6 = [cur_week - timedelta(weeks=i) for i in range(7, 13)]
    L.append("| Trend | Prior 6 wk | Last 6 wk | Change | Verdict |")
    L.append("|---|---|---|---|---|")
    flagged = []

    def judge(key, label, a_vals, b_vals, thr, sign, kind, show):
        if not a_vals or not b_vals:
            L.append(f"| {label} | -- | -- | -- | not enough data |")
            return
        a, b = statistics.mean(a_vals), statistics.mean(b_vals)
        delta = (b - a) / a if kind == "rel" and a else b - a
        if abs(delta) < thr:
            verdict = "plateau"
        else:
            verdict = "improving" if delta * sign > 0 else "declining"
        st = state.get(key, {})
        if verdict == "plateau":
            since = st.get("plateau_since") or today.isoformat()
            st["plateau_since"] = since
            if date.fromisoformat(since) <= today - timedelta(days=28):
                verdict = f"PLATEAU (since {since})"
                flagged.append(key)
        else:
            st.pop("plateau_since", None)
        st["last"] = verdict
        st["checked"] = today.isoformat()
        state[key] = st
        ch = f"{delta:+.1%}" if kind == "rel" else f"{delta:+.1f}"
        L.append(f"| {label} | {show(a)} | {show(b)} | {ch} | {verdict} |")

    for key, (label, thr, sign, kind) in METRICS.items():
        src = "km" if key in ("weekly_km", "long_run") else key
        if key == "weekly_km":
            av = [wk_value(w, "km") or 0 for w in prior6]
            bv = [wk_value(w, "km") or 0 for w in recent6]
            show = lambda v: f"{v:.1f} km"
        elif key == "long_run":
            av = [max(weekly[w].get("long") or [0]) for w in prior6]
            bv = [max(weekly[w].get("long") or [0]) for w in recent6]
            show = lambda v: f"{v:.1f} km"
        else:
            av = [x for w in prior6 for x in (weekly[w].get(src) or [])]
            bv = [x for w in recent6 for x in (weekly[w].get(src) or [])]
            if key.startswith("ef_"):
                show = lambda v: fmt_pace(ef_to_pace(v)) + "/km"
            else:
                show = lambda v: f"{v:.1f}"
        judge(key, label, av, bv, thr, sign, kind, show)
    for label, s in lifts.items():
        a = [v for d, v in s.items() if prior6[-1] <= date.fromisoformat(d) < recent6[-1]]
        b = [v for d, v in s.items() if date.fromisoformat(d) >= recent6[-1]]
        judge(f"lift_{label}", f"{label} e1RM", [max(a)] if a else [], [max(b)] if b else [],
              0.025, +1, "rel", lambda v: f"{v:.0f} kg")

    if flagged:
        L.append("\n**Flagged plateaus and the breakthrough to discuss with the athlete:**\n")
        for key in flagged:
            L.append(f"- {METRICS[key][0] if key in METRICS else key}: "
                     f"{PLAYBOOK.get(key, PLAYBOOK['strength'] if key.startswith('lift_') else '')}")
    else:
        L.append("\nNo plateau has held for 4+ weeks.")
    L.append("\nHow to read: a single 6-week plateau is normal; it is flagged only after it holds for "
             "4+ weeks. \"Everything flat and HRV down\" means recovery or fuel is the limiter (P30).")

    text = "\n".join(L) + "\n"
    print(text)
    if not args.no_write:
        with open(OUT_MD, "w", encoding="utf-8") as f:
            f.write(text)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        print(f"[written] {OUT_MD}")


if __name__ == "__main__":
    main()
