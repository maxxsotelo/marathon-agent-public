"""
sensor_pre_schedule_check.py — the mandatory pre-schedule gate
==============================================================
Run before ANY workout is uploaded to Garmin: runs, lifting and cardio.
actuator_workout_generator.py and actuator_session_uploader.py call it
themselves before every upload.

Exit codes
    0  CLEARED   (for a future date: PROVISIONAL, re-check that morning)
    1  BLOCKED   do not schedule
    2  CONSULT   a nuanced case: bring the athlete the data, pros/cons and research,
                 and upload only after he decides (--consult-approved)

Rules (CLAUDE.md "Hard gates" and "Consult cases"; brain_knowledge_base.md
Rules 10-11). Every number comes from core_common.py.

  ACWR       gate = higher of mechanical (run km) and load (TRIMP, all activities),
             projected with the proposed session.
             > 1.5: block runs, hard sessions and heavy legs. 1.3-1.5: caution.
  HRV (B)    Z4/5 blocked when last night < Garmin balanced-low AND (7-day avg
             below it OR resting HR >= normal + 5). One low night = warning.
             Marathon pace still allowed, MP segment capped at 20 min.
             Lifting on a day option B trips = CONSULT. No overnight reading =
             hard sessions blocked until the watch syncs.
  Recovery   hours since the last hard or long run ended:
             hard / MP session < 24 h: block; 24-48 h: CONSULT.
             heavy legs < 48 h after a long or hard run: CONSULT.
  Long run   heavy legs within 72 h before a planned long run: block.
  Readiness  TRS < 50: Zone 2 cap (hard and MP blocked).
  Form       FBI < 70 on the last outdoor run: warning (shorten, add mobility).
  Lifting    > 12 hard sets for a muscle in 7 days, or no lifting-free day in
             the last 7: warning.

Usage:
    python sensor_pre_schedule_check.py --type <type> --duration MINS
        [--intensity recovery|easy|marathon|threshold|vo2max] [--mp-minutes N]
        [--date YYYY-MM-DD] [--json]
    types: easy long_run tempo intervals recovery   (runs)
           legs_heavy legs_light upper_body core     (lifting)
           hiit easy_cardio                          (cardio: SkiErg, bike, ...)
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from core_common import (ACWR_CAUTION, ACWR_VETO, PHT, acwr, activities_window, describe_hrv,
                         hrv_status, is_run, last_hard_or_long, now_pht, readiness, run_km,
                         today_pht, trimp)
from garmin_auth import get_client

try:
    from brain_current_week_plan import plan
    PLAN_ERROR = None
except Exception as e:  # the gate still runs without a plan
    plan, PLAN_ERROR = None, str(e)

RUN_TYPES = ["easy", "long_run", "tempo", "intervals", "recovery"]
LIFT_TYPES = ["legs_heavy", "legs_light", "upper_body", "core"]
CARDIO_TYPES = ["hiit", "easy_cardio"]
INTENSITIES = ["recovery", "easy", "marathon", "threshold", "vo2max"]

# Minutes per km by intensity (Oct 2026 paces incl. warm-up/cool-down), for projected km
PACE_MIN_PER_KM = {"recovery": 6.25, "easy": 5.5, "marathon": 4.9, "threshold": 4.6, "vo2max": 4.4}
# Typical session-average HR, for projected TRIMP
SESSION_HR = {"recovery": 135, "easy": 148, "marathon": 165, "threshold": 172, "vo2max": 168,
              "legs_heavy": 110, "legs_light": 100, "upper_body": 100, "core": 95,
              "hiit": 150, "easy_cardio": 120}
MP_CAP_LOW_HRV_MIN = 20
HARD_SETS_WARN = 12
TRS_ZONE2_CAP = 50
FBI_WARN = 70


class Gate:
    def __init__(self):
        self.items = []   # (level, section, message)

    def add(self, level, section, msg):
        self.items.append((level, section, msg))
        tag = {"OK": "  [OK]     ", "INFO": "  [INFO]   ", "WARN": "  [WARN]   ",
               "CONSULT": "  [CONSULT]", "BLOCK": "  [BLOCK]  "}[level]
        print(f"{tag} {msg}")

    def levels(self, level):
        return [m for lv, _, m in self.items if lv == level]


def long_runs_planned(client, start: date, days: int) -> list[str]:
    """Long runs in brain_current_week_plan.py or on the Garmin calendar in [start, start+days]."""
    found = []
    end = start + timedelta(days=days)
    if plan:
        for d, entry in plan.get("days", {}).items():
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            text = f"{entry.get('label', '')} {entry.get('session', '')}".lower()
            if start <= dd <= end and ("long" in text or (entry.get("run_km") or 0) >= 16):
                found.append(f"{d} (week plan: {entry.get('session', '')[:50]})")
    months = {(start.year, start.month), (end.year, end.month)}
    for y, m in months:
        try:
            cal = client.get_scheduled_workouts(y, m) or {}
        except Exception:
            continue
        for item in cal.get("calendarItems") or []:
            if item.get("itemType") != "workout":
                continue
            title = item.get("title") or ""
            try:
                dd = date.fromisoformat(item.get("date", ""))
            except ValueError:
                continue
            if start <= dd <= end and ("long" in title.lower() or " lr" in f" {title.lower()}"):
                found.append(f"{dd} (Garmin: {title})")
    return sorted(set(found))


def lifting_checks(client, g: Gate, target: date):
    try:
        from core_strength_progression import fetch_sessions, weekly_hard_sets
        history = fetch_sessions(client, 14)
        counts = weekly_hard_sets(history, target)
        heavy = {m: n for m, n in counts.items() if n > HARD_SETS_WARN and m not in ("core", "other")}
        if heavy:
            g.add("WARN", "lifting", "Above 12 hard sets in 7 days (extra mass and fatigue): " +
                  ", ".join(f"{m} {n}" for m, n in heavy.items()))
        else:
            g.add("OK", "lifting", "Weekly hard sets within Rule 10 (<= 12 per muscle).")
    except Exception as e:
        g.add("WARN", "lifting", f"Could not count weekly hard sets: {e}")
    try:
        acts = activities_window(client, target, days=7)
        lift_days = {a["startTimeLocal"][:10] for a in acts
                     if a.get("activityType", {}).get("typeKey") == "strength_training"}
        lift_days.add(target.isoformat())
        if len(lift_days) >= 7:
            g.add("WARN", "lifting", "No lifting-free day in the last 7 days (Rule 10 wants at least one).")
    except Exception as e:
        g.add("WARN", "lifting", f"Could not check lifting-free days: {e}")


def main():
    ap = argparse.ArgumentParser(description="Mandatory pre-schedule gate.")
    ap.add_argument("--type", required=True, choices=RUN_TYPES + LIFT_TYPES + CARDIO_TYPES)
    ap.add_argument("--duration", required=True, type=int, help="minutes")
    ap.add_argument("--intensity", default="easy", choices=INTENSITIES, help="runs only")
    ap.add_argument("--mp-minutes", type=int, default=None, help="minutes at marathon pace")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, PHT)")
    ap.add_argument("--json", action="store_true", help="print a JSON verdict as the last line")
    args = ap.parse_args()

    today = today_pht()
    target = date.fromisoformat(args.date) if args.date else today
    future = target > today
    t = args.type
    run = t in RUN_TYPES
    lift = t in LIFT_TYPES
    hard = t in ("tempo", "intervals", "hiit") or (run and args.intensity in ("threshold", "vo2max"))
    mp = run and args.intensity == "marathon"
    intensity = args.intensity if run else t

    print("=" * 70)
    print("  PRE-SCHEDULE GATE")
    print(f"  {target} | {t} {args.duration} min" + (f" @ {args.intensity}" if run else "") +
          (f" | MP {args.mp_minutes} min" if args.mp_minutes else "") +
          (" | PROVISIONAL (future date)" if future else ""))
    print("=" * 70)

    g = Gate()
    client = get_client()
    try:
        acts = activities_window(client, today, days=29)
    except Exception as e:   # each check below then fails closed for hard sessions
        print(f"  [WARN] Could not fetch activities: {e}")
        acts = None

    # ── 1. Week plan ──────────────────────────────────────────────────────
    print("\n[1] WEEK PLAN")
    if plan is None:
        g.add("WARN", "plan", f"Could not load brain_current_week_plan.py: {PLAN_ERROR}")
    else:
        entry = plan.get("days", {}).get(target.isoformat())
        if entry:
            g.add("INFO", "plan", f"{entry.get('label', '')}: {entry.get('session', '')}")
            if run and entry.get("run_km") == 0:
                g.add("WARN", "plan", "The week plan has no run on this day: confirm this is deliberate.")
        else:
            g.add("WARN", "plan", f"No plan entry for {target} (week: {plan.get('meta', {}).get('week')}).")

    # ── 2. ACWR ───────────────────────────────────────────────────────────
    print("\n[2] ACWR (higher of mechanical km and TRIMP load, projected)")
    extra_km = args.duration / PACE_MIN_PER_KM[args.intensity] if run else 0.0
    extra_trimp = trimp(args.duration, SESSION_HR[intensity])
    # Future date: the 7-day window ends on the session's date, so add the runs the week
    # plan has between today and then (as easy runs) on top of what Garmin already has.
    planned_km = 0.0
    if future and plan:
        for d, entry in plan.get("days", {}).items():
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if today < dd < target and dd >= target - timedelta(days=6):
                planned_km += float(entry.get("run_km") or 0)
    planned_trimp = trimp(planned_km * PACE_MIN_PER_KM["easy"], SESSION_HR["easy"])
    end = target if future else today
    try:
        now_a = acwr(client, today, acts=acts)
        proj = acwr(client, end, extra_km=extra_km + planned_km,
                    extra_trimp=extra_trimp + planned_trimp, acts=acts)
        print(f"  now: mechanical {now_a['mechanical']} | load {now_a['load']} "
              f"(7 d {now_a['acute_km']} km vs {now_a['chronic_km_week']} km/wk)")
        if future:
            print(f"  on {target}: 7-day window {target - timedelta(days=6)} to {target}, incl. "
                  f"{planned_km:.1f} km planned in the week plan before the session")
        print(f"  with this session: mechanical {proj['mechanical']} | load {proj['load']} "
              f"-> gate {proj['gate']}")
        if proj["gate"] > ACWR_VETO:
            if run or hard or t == "legs_heavy":
                g.add("BLOCK", "acwr", f"Projected ACWR {proj['gate']} > {ACWR_VETO}.")
            else:
                g.add("WARN", "acwr", f"Projected ACWR {proj['gate']} > {ACWR_VETO}: keep it light.")
        elif proj["gate"] > ACWR_CAUTION:
            g.add("WARN", "acwr", f"Projected ACWR {proj['gate']} in caution zone (1.3-1.5): "
                                  "hold volume for 3-5 days.")
        elif proj["gate"] < 0.8:
            g.add("INFO", "acwr", f"Projected ACWR {proj['gate']} < 0.8: underloaded, adding volume is fine.")
        else:
            g.add("OK", "acwr", f"Projected ACWR {proj['gate']} in the 0.8-1.3 sweet spot.")
    except Exception as e:
        lvl = "BLOCK" if (hard or t == "legs_heavy") else "WARN"
        g.add(lvl, "acwr", f"Could not compute ACWR ({e}); hard sessions are blocked until it can.")

    # ── 3. HRV, option B ──────────────────────────────────────────────────
    print("\n[3] HRV (option B)")
    if future:
        g.add("INFO", "hrv", f"Future date: HRV is checked on the morning of {target} (PROVISIONAL).")
    else:
        h = hrv_status(client, target)
        print(f"  {describe_hrv(h)}")
        if h["missing"]:
            if hard:
                g.add("BLOCK", "hrv", "No overnight HRV: sync the watch and re-run the gate.")
            else:
                g.add("WARN", "hrv", "No overnight HRV reading.")
        elif h["trips"]:
            if hard:
                g.add("BLOCK", "hrv", "Option B tripped: no Zone 4/5 today.")
            elif mp:
                if args.mp_minutes and args.mp_minutes > MP_CAP_LOW_HRV_MIN:
                    g.add("BLOCK", "hrv", f"Option B tripped: marathon-pace segment over "
                                          f"{MP_CAP_LOW_HRV_MIN} min.")
                else:
                    g.add("WARN", "hrv", f"Option B tripped: keep the MP segment <= {MP_CAP_LOW_HRV_MIN} min.")
            elif lift:
                g.add("CONSULT", "hrv", "Option B tripped on a lifting day: consult the athlete (volume, intensity).")
            else:
                g.add("OK", "hrv", "Option B tripped, but this session is not Zone 4/5.")
        elif h["low_night"]:
            g.add("WARN", "hrv", "Single low night (7-day average and resting HR normal): warning only.")
        else:
            g.add("OK", "hrv", "HRV above the floor.")
        if not h["missing"] and h["trend"]:
            g.add("WARN", "hrv", f"Rule 11 trend flag: 4-week average {h['avg_4wk']} or balanced-low "
                                 f"{h.get('floor')} is low. Review recovery and fuelling this week.")

    # ── 4. Recovery since the last hard or long run ───────────────────────
    print("\n[4] RECOVERY (hours since the last hard or long run ended)")
    at = now_pht() if target == today else datetime.combine(target, time(6, 0), tzinfo=PHT)
    try:
        last = last_hard_or_long(client, at, acts=acts)
        recovery_error = None
    except Exception as e:
        last, recovery_error = None, e
    if recovery_error is not None:
        lvl = "BLOCK" if (hard or mp or t == "legs_heavy") else "WARN"
        g.add(lvl, "recovery", f"Could not read recent runs ({recovery_error}); hard sessions blocked.")
    elif not last:
        g.add("OK", "recovery", "No hard or long run in the last 10 days.")
    else:
        h_since = last["hours"]
        desc = (f"{h_since:.0f} h since a {last['kind']} run ({last['km']} km, TE {last['te']}, "
                f"ended {last['end']:%a %b %d %H:%M})")
        print(f"  {desc}")
        if hard or mp:
            if h_since < 24:
                g.add("BLOCK", "recovery", f"Hard/MP session < 24 h after a {last['kind']} run.")
            elif h_since < 48:
                g.add("CONSULT", "recovery", f"Hard/MP session 24-48 h after a {last['kind']} run: consult the athlete.")
            else:
                g.add("OK", "recovery", "48 h+ since the last hard or long run.")
        elif t == "legs_heavy" and h_since < 48:
            g.add("CONSULT", "recovery", f"Heavy legs < 48 h after a {last['kind']} run: consult the athlete.")
        elif t == "long_run" and h_since < 48 and last["kind"] == "long":
            g.add("WARN", "recovery", "Long run < 48 h after another long run (back-to-back): deliberate?")
        elif run and h_since < 6 and t != "recovery":
            g.add("WARN", "recovery", "< 6 h after a hard or long run: Zone 1 only.")
        else:
            g.add("OK", "recovery", desc)

    # ── 5. Readiness and form ─────────────────────────────────────────────
    if run or hard:
        print("\n[5] READINESS (TRS) AND FORM (FBI)")
        if future:
            g.add("INFO", "readiness", "Future date: TRS is checked that morning (PROVISIONAL).")
        else:
            r = readiness(client, target, acts=acts)
            if r["trs"] is None:
                g.add("WARN", "readiness", f"TRS unavailable {r['notes']}")
            elif r["trs"] < TRS_ZONE2_CAP and (hard or mp):
                g.add("BLOCK", "readiness", f"TRS {r['trs']} < {TRS_ZONE2_CAP}: Zone 2 cap today.")
            else:
                g.add("OK", "readiness", f"TRS {r['trs']}.")
            if r["fbi"] is not None and r["fbi"] < FBI_WARN:
                g.add("WARN", "form", f"FBI {r['fbi']} on the last outdoor run ({r.get('fbi_run')}): "
                                      "shorten the session and add mobility. (FBI is not pace-corrected: "
                                      "a slower finish lowers it.)")

    # ── 6. Lifting ────────────────────────────────────────────────────────
    if lift:
        print("\n[6] LIFTING")
        if t in ("legs_heavy", "legs_light"):
            longs = long_runs_planned(client, target + timedelta(days=1), 3)
            if longs and t == "legs_heavy":
                g.add("BLOCK", "lifting", f"Heavy legs within 72 h before a long run: {', '.join(longs)}.")
            elif longs:
                g.add("WARN", "lifting", f"Long run within 72 h ({', '.join(longs)}): keep legs light (RPE 6).")
            else:
                g.add("OK", "lifting", "No long run planned in the next 72 h.")
        lifting_checks(client, g, target)

    # ── Verdict ───────────────────────────────────────────────────────────
    blocks, consults = g.levels("BLOCK"), g.levels("CONSULT")
    print("\n" + "=" * 70)
    if blocks:
        verdict, code = "BLOCKED", 1
    elif consults:
        verdict, code = "CONSULT MAX", 2
    else:
        verdict, code = "CLEARED" + (f" (PROVISIONAL: re-check on the morning of {target})" if future else ""), 0
    print(f"  VERDICT: {verdict}")
    for m in blocks:
        print(f"    block:   {m}")
    for m in consults:
        print(f"    consult: {m}")
    for m in g.levels("WARN"):
        print(f"    warning: {m}")
    print("=" * 70)
    if args.json:
        print(json.dumps({"verdict": verdict, "code": code, "provisional": future,
                          "items": [{"level": lv, "section": s, "message": m} for lv, s, m in g.items]}))
    sys.exit(code)


if __name__ == "__main__":
    main()
