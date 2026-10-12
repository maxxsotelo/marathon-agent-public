"""
Strength progression engine: best sets, estimated 1RM and next-session targets
per exercise, read from Garmin strength sessions.

This script only proposes numbers. It never schedules anything: the coach fits
its targets around the running plan (brain_knowledge_base.md Rule 10, the 72 h
heavy-leg rule and the HRV gate), and running stays the priority.

Progression rule (double progression, Rule 10 rep ranges):
  - Working sets are sets at >= 90% of that session's heaviest weight.
  - Every working set reached the top of the rep range -> add one increment.
  - Top set inside the range -> keep the weight, add a rep.
  - Top set below the range two sessions running -> drop ~5%.
  - Last done more than 21 days ago -> restart at ~90% of the last top weight.

Weights are as logged on the watch (dumbbells per hand, plate-loaded machines as
the total the athlete entered). Sets over 10 reps are left out of the 1RM estimate
because Epley overstates them (and merged watch sets show up as 15-17 reps).

Usage:
    python core_strength_progression.py [--days N] [--day push|pull|legs|core]
                                        [--exercise NAME_SUBSTRING]
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from core_common import PHT
from garmin_auth import get_client

STALE_DAYS = 21
RECENT_DAYS = 45   # default view: exercises done in this window (--all shows everything)

# Sessions whose logged weights are known to be wrong; left out of bests and targets.
BAD_SESSIONS = {
    ("DUMBBELL_BENCH_PRESS", "2026-07-27"): "logged in lb as pair totals (real: ~41 kg/hand x4)",
}

# Rep ranges from Rule 10: main lifts 3-6, accessories 6-12, prehab/isolation higher.
MAIN = (3, 6)
ACCESSORY = (6, 10)
ISOLATION = (8, 12)
PREHAB = (12, 15)

MAIN_NAMES = {
    "BARBELL_BENCH_PRESS", "SMITH_MACHINE_BENCH_PRESS", "LAT_PULLDOWN", "WEIGHTED_PULL_UP",
    "SEATED_CABLE_ROW", "BARBELL_ROW", "CHEST_SUPPORTED_DUMBBELL_ROW", "OVERHEAD_BARBELL_PRESS",
    "BARBELL_BACK_SQUAT", "FRONT_SQUAT", "BARBELL_DEADLIFT", "ROMANIAN_DEADLIFT", "TRAP_BAR_DEADLIFT",
}
PREHAB_NAMES = {"FACE_PULL", "REVERSE_FLYE", "SEATED_LATERAL_RAISE", "CABLE_CORE_PRESS"}
PREHAB_CATEGORIES = {"LATERAL_RAISE", "CORE", "PLANK", "HIP_STABILITY", "CRUNCH", "LEG_RAISE", "CALF_RAISE"}
ISOLATION_CATEGORIES = {"CURL", "TRICEPS_EXTENSION", "FLYE", "LEG_CURL"}

MUSCLE = {
    "BENCH_PRESS": "chest", "PUSH_UP": "chest", "FLYE": "chest",
    "SHOULDER_PRESS": "shoulders", "LATERAL_RAISE": "shoulders",
    "TRICEPS_EXTENSION": "triceps", "CURL": "biceps",
    "PULL_UP": "back", "ROW": "back",
    "SQUAT": "legs", "LUNGE": "legs", "DEADLIFT": "legs", "LEG_CURL": "legs",
    "CALF_RAISE": "legs", "HIP_RAISE": "legs",
    "CORE": "core", "PLANK": "core", "CRUNCH": "core", "LEG_RAISE": "core", "HIP_STABILITY": "core",
}
REAR_DELT_NAMES = {"FACE_PULL", "REVERSE_FLYE"}
MAJOR_MUSCLES = {"chest", "back", "shoulders", "legs"}

DAY_MUSCLES = {
    "push": {"chest", "shoulders", "triceps"},
    "pull": {"back", "rear delts", "biceps"},
    "legs": {"legs"},
    "core": {"core"},
}


def rep_range(category, name):
    if name in MAIN_NAMES:
        return MAIN
    if name in PREHAB_NAMES or category in PREHAB_CATEGORIES:
        return PREHAB
    if category in ISOLATION_CATEGORIES:
        return ISOLATION
    return ACCESSORY


def muscle(category, name):
    if name in REAR_DELT_NAMES:
        return "rear delts"
    return MUSCLE.get(category, "other")


def increment(category, name, weight):
    if category in ("SQUAT", "DEADLIFT") or "LEG_PRESS" in name:
        return 5.0
    if "DUMBBELL" in name:
        return 2.5 if weight >= 10 else 1.0
    return 2.5   # barbell, Smith, cable stack (or the next pin)


def round_half(x):
    return round(x * 2) / 2


def epley(weight, reps):
    return weight * (1 + reps / 30)


def fetch_sessions(client, days):
    """Return {exercise_key: [session, ...]} oldest first. A session is
    {date, category, name, sets: [(weight_kg, reps)]}."""
    end = datetime.now(PHT).date()
    start = end - timedelta(days=days)
    acts = [a for a in client.get_activities_by_date(start.isoformat(), end.isoformat())
            if a.get("activityType", {}).get("typeKey") == "strength_training"]
    acts.sort(key=lambda a: a.get("startTimeLocal", ""))

    history = defaultdict(list)
    for act in acts:
        day = act.get("startTimeLocal", "")[:10]
        data = client.get_activity_exercise_sets(act["activityId"]) or {}
        per_ex = {}
        for s in data.get("exerciseSets") or []:
            if s.get("setType") != "ACTIVE" or not s.get("repetitionCount"):
                continue
            ex = (s.get("exercises") or [{}])[0]
            category = ex.get("category") or "UNKNOWN"
            name = ex.get("name") or category
            if category in ("CARDIO", "WARM_UP", "PLYO"):
                continue
            weight = (s.get("weight") or 0) / 1000.0
            if (name, day) in BAD_SESSIONS:
                continue
            sess = per_ex.setdefault(name, {"date": day, "category": category, "name": name, "sets": []})
            sess["sets"].append((weight, int(s["repetitionCount"])))
        for name, sess in per_ex.items():
            history[name].append(sess)
    return history


def is_generic(category, name):
    """Sets the watch logged with only a category (or nothing): they mix different
    exercises, so their bests and targets mean nothing."""
    return name == category or category == "UNKNOWN"


def working_sets(sets):
    top = max(w for w, _ in sets)
    return [(w, r) for w, r in sets if w >= 0.9 * top] if top else sets


def next_target(sessions, today):
    last = sessions[-1]
    category, name = last["category"], last["name"]
    lo, hi = rep_range(category, name)
    work = working_sets(last["sets"])
    top_w = max(w for w, _ in work)
    top_reps = max(r for w, r in work if w == top_w)
    age = (today - datetime.strptime(last["date"], "%Y-%m-%d").date()).days

    if top_w == 0:   # bodyweight: progress reps, then load
        if top_reps <= 2:
            return "timed hold: progress the time", top_w
        if top_reps >= hi:
            return f"bodyweight {top_reps} reps: add load or a harder variation", top_w
        return f"bodyweight: aim {top_reps + 1} reps", top_w
    if age > STALE_DAYS:
        w = round_half(top_w * 0.9)
        return f"{w:g} kg x {lo}-{hi} (restart: last done {age} days ago)", w
    if all(r >= hi for _, r in work):
        inc = increment(category, name, top_w)
        w = round_half(top_w + inc)
        min_reps = min(r for w, r in work if w == top_w)   # merged watch sets inflate the max
        if min_reps > hi + 2:
            # Far above the range: aim for the weight that matches the top of the
            # range by Epley, capped at +10% per session.
            by_e1rm = epley(top_w, min(min_reps, 15)) / (1 + hi / 30)
            w = max(w, min(round_half(round(by_e1rm / inc) * inc), round_half(top_w * 1.10)))
        return f"{w:g} kg x {lo}-{hi} (+{w - top_w:g} kg: hit {hi}+ on every working set)", w
    if top_reps < lo:
        prev_below = False
        if len(sessions) > 1:
            prev = working_sets(sessions[-2]["sets"])
            prev_top = max(w for w, _ in prev)
            prev_below = max(r for w, r in prev if w == prev_top) < lo
        if prev_below:
            w = round_half(top_w * 0.95)
            return f"{w:g} kg x {lo}-{hi} (-5%: below {lo} reps two sessions running)", w
        return f"{top_w:g} kg, aim {lo}+ reps (below range last time)", top_w
    return f"{top_w:g} kg, aim {min(top_reps + 1, hi)} reps (same weight, +1 rep)", top_w


def weekly_hard_sets(history, today):
    """Hard sets per muscle in the last 7 days (sets at >= 75% of that session's top weight)."""
    since = today - timedelta(days=6)
    counts = defaultdict(int)
    for sessions in history.values():
        for sess in sessions:
            if datetime.strptime(sess["date"], "%Y-%m-%d").date() < since:
                continue
            top = max(w for w, _ in sess["sets"])
            n = sum(1 for w, _ in sess["sets"] if not top or w >= 0.75 * top)
            m = "other" if is_generic(sess["category"], sess["name"]) and sess["category"] not in MUSCLE \
                else muscle(sess["category"], sess["name"])
            counts[m] += n
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=180, help="history window (default 180)")
    ap.add_argument("--day", choices=sorted(DAY_MUSCLES), help="only exercises for this session type")
    ap.add_argument("--exercise", help="only exercises whose name contains this text")
    ap.add_argument("--all", action="store_true",
                    help=f"include exercises not done in the last {RECENT_DAYS} days and unnamed watch sets")
    args = ap.parse_args()

    today = datetime.now(PHT).date()
    history = fetch_sessions(get_client(), args.days)
    if not history:
        print(f"No strength sessions with logged sets in the last {args.days} days.")
        return

    rows = []
    for name, sessions in history.items():
        last = sessions[-1]
        m = muscle(last["category"], name)
        age = (today - datetime.strptime(last["date"], "%Y-%m-%d").date()).days
        if not args.all and (age > RECENT_DAYS or is_generic(last["category"], name)):
            continue
        if args.day and m not in DAY_MUSCLES[args.day]:
            continue
        if args.exercise and args.exercise.upper() not in name.upper():
            continue
        best = max(((w, r) for s in sessions for w, r in s["sets"] if w),
                   key=lambda wr: (wr[0], wr[1]), default=None)
        e1rm = max((epley(w, r) for s in sessions for w, r in s["sets"] if w and r <= 10), default=0)
        work = working_sets(last["sets"])
        last_str = ", ".join(f"{w:g}x{r}" if w else f"BWx{r}" for w, r in work)
        target, _ = next_target(sessions, today)
        lo, hi = rep_range(last["category"], name)
        rows.append((m, name, len(sessions), best, e1rm, last["date"], last_str, f"{lo}-{hi}", target))

    rows.sort(key=lambda r: (r[0], -r[4], r[1]))
    print(f"=== STRENGTH PROGRESSION ({today}, last {args.days} days"
          f"{', ' + args.day if args.day else ''}) ===")
    print("Targets are proposals only: fit them around the run plan (Rule 10, HRV gate, 72 h leg rule).\n")
    current = None
    for m, name, n, best, e1rm, date, last_str, rng, target in rows:
        if m != current:
            print(f"--- {m.upper()} ---")
            current = m
        best_str = f"{best[0]:g}x{best[1]}" if best else "BW"
        e1rm_str = f"e1RM {e1rm:.0f}" if e1rm else "e1RM --"
        print(f"{name.replace('_', ' ').title()}  [{n} sessions, best {best_str}, {e1rm_str}, range {rng}]")
        print(f"    last {date}: {last_str}")
        print(f"    next: {target}")

    counts = weekly_hard_sets(history, today)
    print("\n=== HARD SETS PER MUSCLE, LAST 7 DAYS (Rule 10 target 8-12) ===")
    for m in sorted(counts):
        n = counts[m]
        note = ""
        if m in ("core", "other"):
            note = "  (not counted against the target)"
        elif n > 12:
            note = "  << above 12: extra volume adds mass and fatigue"
        elif n < 8 and m in MAJOR_MUSCLES:
            note = "  (below 8)"
        print(f"{m:<11}{n:3d}{note}")
    print("Arms and rear delts also get indirect work from presses and pulls; no minimum for them.")


if __name__ == "__main__":
    main()
