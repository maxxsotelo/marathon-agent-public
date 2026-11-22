"""
actuator_session_uploader.py — upload any lifting, cardio or treadmill/run session
=================================================================================
One uploader for everything actuator_workout_generator.py doesn't build. The
session is described in a small JSON spec; the uploader

  1. runs the mandatory gate (sensor_pre_schedule_check.py) with the spec's
     "gate" block: exit 1 stops, exit 2 (CONSULT) stops unless the athlete has decided
     and --consult-approved is given (logged to brain_decision_log.md);
  2. builds the Garmin workout: exercise-library names (actuator_garmin_exercises.py
     keys or "CATEGORY/EXERCISE_NAME"), weights ("auto" = next target from
     core_strength_progression.py), rests ("auto" = Rule 10: 2:30 heavy compounds,
     2:00 other hard sets, 1:15 isolation/prehab);
  3. uploads it, checks Garmin kept every exercise name, and schedules it.

Usage:
    python actuator_session_uploader.py SPEC.json [--dry-run] [--consult-approved "the athlete's decision"]

Spec (strength):
    {"name": "W17D2: Upper Pull [Tue]", "date": "2026-10-13", "sport": "strength",
     "description": "...", "gate": {"type": "upper_body", "duration": 50},
     "warmup": {"secs": 300, "note": "5 min row + band pull-aparts"},
     "exercises": [
        {"exercise": "LAT_PULLDOWN", "sets": 4, "reps": 5, "weight": "auto"},
        {"exercise": "PULL_UP/LAT_PULLDOWN", "sets": 3, "reps": 8, "weight": 60, "rest": 120,
         "note": "wide grip"},
        {"exercise": "single_leg_rdl", "sets": 2, "reps": 8, "weight": 16, "per_side": true},
        {"superset": [{"exercise": "seated_lateral_raise", "reps": 12, "weight": 12.5},
                      {"exercise": "db_hammer_curl", "reps": 10, "weight": 15}],
         "sets": 3, "rest": 75},
        {"exercise": "side_plank", "sets": 2, "secs": 30, "per_side": true}]}
    A "superset" runs its members back to back for "sets" rounds ("switch" seconds between
    members, default 15; "rest" after each round, "auto" = the longest Rule 10 rest of the
    members). "secs" instead of "reps" makes a timed hold.

Spec (cardio, e.g. SkiErg; or run, e.g. treadmill: time-based with belt speeds in the notes):
    {"name": "...", "date": "...", "sport": "cardio" | "run", "gate": {"type": "hiit", "duration": 25},
     "steps": [{"kind": "warmup", "secs": 300, "note": "easy"},
               {"repeat": 6, "steps": [{"kind": "interval", "secs": 60, "note": "hard", "hr": [165, 185]},
                                        {"kind": "recovery", "secs": 90, "note": "easy"}]},
               {"kind": "cooldown", "secs": 300}]}
    Run steps may use "meters" instead of "secs" (outdoor only: the watch under-reads
    treadmill distance, so treadmill steps must be time-based).
    Short efforts (surges, strides: up to 2 min or 400 m) get no HR target even if "hr" is given:
    heart rate lags 30-60 s, so the watch would beep "too low" for most of the step and push the
    pace up to chase it. The range moves into the step note; run them by feel.
"""

import argparse
import json
import os
import subprocess
import sys

from actuator_garmin_exercises import ALL_EXERCISES, KG_UNIT

SPORTS = {
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "cardio": {"sportTypeId": 6, "sportTypeKey": "cardio_training"},
    "run": {"sportTypeId": 1, "sportTypeKey": "running"},
}
STEP_TYPES = {"warmup": (1, "warmup"), "cooldown": (2, "cooldown"), "interval": (3, "interval"),
              "recovery": (4, "recovery"), "rest": (5, "rest"), "repeat": (6, "repeat")}
TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
DIST = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
HR_TARGET = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}

REST_HEAVY, REST_HARD, REST_LIGHT = 150, 120, 75   # Rule 10 rest periods (seconds)
SHORT_STEP_SECS, SHORT_STEP_METERS = 120, 400       # no HR target on efforts this short (HR lags)


def resolve_exercise(ref: str) -> tuple[str, str | None]:
    """actuator_garmin_exercises key, 'CATEGORY/NAME', or a bare Garmin exercise name."""
    if ref in ALL_EXERCISES:
        e = ALL_EXERCISES[ref]
        return e["category"], e["exerciseName"]
    if "/" in ref:
        cat, name = ref.split("/", 1)
        return cat, name or None
    for e in ALL_EXERCISES.values():
        if e["exerciseName"] == ref:
            return e["category"], ref
    raise SystemExit(f"[UPLOADER] Unknown exercise '{ref}': use a key from actuator_garmin_exercises.py "
                     "or CATEGORY/EXERCISE_NAME (round-trip check it first).")


def auto_rest(category: str, name: str | None) -> int:
    from core_strength_progression import MAIN, PREHAB, ISOLATION, rep_range
    rng = rep_range(category, name or category)
    if rng == MAIN:
        return REST_HEAVY
    if rng in (PREHAB, ISOLATION):
        return REST_LIGHT
    return REST_HARD


class Steps:
    def __init__(self):
        self.order = 0
        self.steps = []

    def _next(self):
        self.order += 1
        return self.order

    def exec_step(self, kind, desc, end, value, target=None, extra=None):
        tid, tkey = STEP_TYPES[kind]
        s = {"type": "ExecutableStepDTO", "stepOrder": self._next(),
             "stepType": {"stepTypeId": tid, "stepTypeKey": tkey, "displayOrder": tid},
             "description": desc[:500], "endCondition": end, "endConditionValue": float(value),
             "targetType": NO_TARGET}
        if target:
            s.update({"targetType": HR_TARGET, "targetValueOne": float(target[0]),
                      "targetValueTwo": float(target[1])})
        s.update(extra or {})
        return s


def build_strength(spec, client) -> list:
    st = Steps()
    out = []
    if spec.get("warmup"):
        w = spec["warmup"]
        out.append(st.exec_step("warmup", w.get("note", "Warm-up"), TIME, w.get("secs", 300)))
    targets = {}

    def resolve(ex):
        cat, name = resolve_exercise(ex["exercise"])
        weight = ex.get("weight")
        if weight == "auto":
            if not targets:
                from core_strength_progression import fetch_sessions, next_target
                from core_common import today_pht
                hist = fetch_sessions(client, 180)
                targets.update({n: next_target(s, today_pht()) for n, s in hist.items()})
            key = name or cat
            if key not in targets or not targets[key][1]:
                raise SystemExit(f"[UPLOADER] No Garmin history for {key}: give an explicit weight.")
            weight = targets[key][1]
            print(f"  auto weight {key}: {targets[key][0]}")
        return cat, name, weight

    def one_set(ex, cat, name, weight, i, sets):
        """One set of one exercise (both sides if per_side): reps, or a timed hold with "secs"."""
        label = ex.get("note") or (name or cat).replace("_", " ").title()
        sides = ["Left", "Right"] if ex.get("per_side") else [None]
        for side in sides:
            extra = {"category": cat, "exerciseName": name}
            if weight:
                extra.update({"weightValue": float(weight), "weightUnit": KG_UNIT})
            desc = f"{label} - Set {i}/{sets}" + (f" {side}" if side else "")
            if ex.get("secs"):
                out.append(st.exec_step("interval", desc, TIME, int(ex["secs"]), extra=extra))
            else:
                out.append(st.exec_step("interval", desc, REPS, int(ex["reps"]), extra=extra))
            if side == "Left":
                out.append(st.exec_step("rest", "Switch sides", TIME, 30))

    for ex in spec["exercises"]:
        if "superset" in ex:
            # Rounds of A, B (, C) back to back; "rest" after each round (Rule 10 rest of the
            # heaviest member when "auto"), "switch" seconds between members (default 15).
            members = [(m, *resolve(m)) for m in ex["superset"]]
            rounds = int(ex["sets"])
            rest = ex.get("rest", "auto")
            rest = max(auto_rest(c, n) for _, c, n, _ in members) if rest == "auto" else int(rest)
            switch = int(ex.get("switch", 15))
            for i in range(1, rounds + 1):
                for j, (m, cat, name, weight) in enumerate(members):
                    one_set(m, cat, name, weight, i, rounds)
                    if j < len(members) - 1 and switch:
                        out.append(st.exec_step("rest", "Next exercise", TIME, switch))
                out.append(st.exec_step("rest", f"Rest {rest}s (end of round {i}/{rounds})", TIME, rest))
            continue
        cat, name, weight = resolve(ex)
        rest = ex.get("rest", "auto")
        rest = auto_rest(cat, name) if rest == "auto" else int(rest)
        sets = int(ex["sets"])
        for i in range(1, sets + 1):
            one_set(ex, cat, name, weight, i, sets)
            out.append(st.exec_step("rest", f"Rest {rest}s", TIME, rest))
    return out


def is_short_effort(kind, end, value) -> bool:
    """A surge or stride: an interval step of up to 2 min or 400 m."""
    if kind != "interval":
        return False
    limit = SHORT_STEP_METERS if end is DIST else SHORT_STEP_SECS
    return float(value) <= limit


def build_timed(spec, sport) -> list:
    st = Steps()

    def one(step):
        if "repeat" in step:
            group = {"type": "RepeatGroupDTO", "stepOrder": st._next(),
                     "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                     "numberOfIterations": int(step["repeat"]), "smartRepeat": False,
                     "endCondition": ITER, "endConditionValue": float(step["repeat"]),
                     "workoutSteps": [one(s) for s in step["steps"]]}
            return group
        kind = step.get("kind", "interval")
        if "meters" in step:
            end, val = DIST, step["meters"]
        else:
            end, val = TIME, step["secs"]
        extra = {"category": "CARDIO", "exerciseName": step.get("exercise")} if sport == "cardio" else None
        note, target = step.get("note", kind.title()), step.get("hr")
        if target and is_short_effort(kind, end, val):
            note = f"{note} (No HR target: HR lags on efforts this short. Go by feel; HR {target[0]:g}-{target[1]:g} by the end is fine.)"
            target = None
        return st.exec_step(kind, note, end, val, target=target, extra=extra)

    return [one(s) for s in spec["steps"]]


def run_gate(spec, args) -> None:
    g = spec.get("gate")
    if not g:
        raise SystemExit("[UPLOADER] The spec needs a 'gate' block (type, duration[, intensity, mp_minutes]).")
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "sensor_pre_schedule_check.py"),
           "--type", g["type"], "--duration", str(g["duration"]), "--date", spec["date"]]
    if g.get("intensity"):
        cmd += ["--intensity", g["intensity"]]
    if g.get("mp_minutes"):
        cmd += ["--mp-minutes", str(g["mp_minutes"])]
    code = subprocess.run(cmd).returncode
    if code == 2 and args.consult_approved:
        from core_common import log_decision
        log_decision(spec["name"], spec["date"], "CONSULT", args.consult_approved)
        print("[GATE] CONSULT approved by the athlete (logged). Uploading.\n")
    elif code == 2:
        raise SystemExit("[GATE] CONSULT: bring the athlete the data, pros/cons and research; re-run with "
                         "--consult-approved \"<his decision>\" once he decides.")
    elif code != 0:
        raise SystemExit("[GATE] BLOCKED. Not uploading.")


def main():
    ap = argparse.ArgumentParser(description="Upload a lifting, cardio or run session from a JSON spec.")
    ap.add_argument("spec")
    ap.add_argument("--dry-run", action="store_true", help="build and print; no gate, no upload")
    ap.add_argument("--consult-approved", metavar="MAX_DECISION", default=None,
                    help="only after the athlete decided a CONSULT in chat (never in headless runs)")
    args = ap.parse_args()

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)
    sport = spec.get("sport", "strength")
    if sport not in SPORTS:
        raise SystemExit(f"[UPLOADER] sport must be one of {list(SPORTS)}")

    client = None
    if not args.dry_run:
        run_gate(spec, args)
    if sport == "strength":
        if any(e.get("weight") == "auto" for e in spec["exercises"]):
            from garmin_auth import get_client
            client = get_client()
        steps = build_strength(spec, client)
    else:
        steps = build_timed(spec, sport)

    workout = {"workoutName": spec["name"][:80], "description": spec.get("description", "")[:1000],
               "sportType": SPORTS[sport], "estimatedDurationInSecs": int(spec["gate"]["duration"]) * 60,
               "workoutSegments": [{"segmentOrder": 1, "sportType": SPORTS[sport], "workoutSteps": steps}]}
    if args.dry_run:
        print(json.dumps(workout, indent=1)[:6000])
        print(f"[DRY RUN] {len(steps)} top-level steps; nothing uploaded.")
        return

    if client is None:
        from garmin_auth import get_client
        client = get_client()
    wid = client.upload_workout(workout)["workoutId"]
    print(f"[UPLOADER] Uploaded workout {wid}: {spec['name']}")

    # Round-trip check: Garmin silently drops exercise names it doesn't know.
    sent = {(s.get("category"), s.get("exerciseName")) for s in steps if s.get("exerciseName")}
    got_steps = client.get_workout_by_id(wid)["workoutSegments"][0]["workoutSteps"]
    kept = {(s.get("category"), s.get("exerciseName")) for s in got_steps if s.get("exerciseName")}
    for cat, name in sorted(sent - kept):
        print(f"  [WARN] Garmin dropped exercise name {cat}/{name}: it shows as a generic {cat} step. "
              "Fix it in actuator_garmin_exercises.py.")
    sched = client.schedule_workout(wid, spec["date"])
    print(f"[UPLOADER] Scheduled for {sched.get('calendarDate', spec['date'])}.")


if __name__ == "__main__":
    main()
