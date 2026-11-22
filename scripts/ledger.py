"""Training data ledger: the compact, complete record every model learns from (build_plan T15).

    python scripts/ledger.py --morning [--date YYYY-MM-DD]     # the morning check: yesterday done, today's plan
    python scripts/ledger.py --backfill --since 2025-08-21     # once, through the Cloud agent
    python scripts/ledger.py --plans                           # rebuild plan.csv from the week plans (no Garmin)

Writes data/ledger/:
  activities.csv  one row per Garmin activity (runs, lifting, cardio, walks): distance, time, HR, TE, TRIMP,
                  climb, wrist temperature -> heat index, cadence, and the athlete's own post-workout rating
                  (rpe 1-10, feel, session-RPE load = rpe x moving minutes, Foster)
  daily.csv       one row per day: HRV (+7-day, floor), resting HR, sleep, Body Battery, weight, VO2max,
                  eaten / exercise kcal and energy availability
  plan.csv        one row per planned day: what was planned (snapshot taken the morning of the day, before
                  the plan file is overwritten with the actual), what was done, and the deviation

Every write is an idempotent upsert (`upsert_csv`): rerunning changes nothing, and a value already in the
ledger is never wiped by an empty one (a rating entered later fills in; nothing is invented, Rule 1).
Garmin login only through garmin_auth.get_client(); a failed call leaves the field empty and says so.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import runpy
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_common import LEAN_MASS_KG, RUN_TYPE_KEYS, today_pht, trimp  # noqa: E402
from core_run_card import TEMP_BIAS_C, heat_index_c  # noqa: E402

LEDGER = ROOT / "data" / "ledger"
ACTIVITY_COLUMNS = ["activity_id", "date", "start", "type", "name", "treadmill", "km", "moving_min", "elapsed_min",
                    "avg_hr", "max_hr", "avg_speed_mps", "aerobic_te", "anaerobic_te", "trimp", "elev_gain_m",
                    "climb_per_km", "temp_min_c", "temp_max_c", "heat_index_c", "avg_cadence", "calories",
                    "rpe", "feel", "srpe_load"]
DAILY_COLUMNS = ["date", "hrv", "hrv_7d", "hrv_floor", "rhr", "sleep_h", "body_battery", "weight_kg", "vo2max",
                 "eaten_kcal", "exercise_kcal", "ea", "steps"]
PLAN_COLUMNS = ["date", "weekday", "planned_type", "planned_km", "planned_label", "plan_source", "done",
                "actual_km", "deviation"]
COLUMNS = {"activities.csv": ACTIVITY_COLUMNS, "daily.csv": DAILY_COLUMNS, "plan.csv": PLAN_COLUMNS}
KEYS = {"activities.csv": "activity_id", "daily.csv": "date", "plan.csv": "date"}

FEEL = {0: "very weak", 25: "weak", 50: "normal", 75: "strong", 100: "very strong"}
TREADMILL_KEYS = ("treadmill_running", "indoor_running")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RUN_GATES = ("long_run", "intervals", "tempo", "easy", "recovery")     # first match wins: the day's run type
LIFT_GATES = ("legs_heavy", "legs_light", "upper_body", "core")


# ── CSV ──────────────────────────────────────────────────────────────────────

def read_rows(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _s(v) -> str:
    return "" if v is None else str(v)


def upsert_csv(path, rows: list[dict], key: str) -> tuple[int, int]:
    """Insert or update rows by `key`; returns (added, updated). Idempotent; sorted by key. A new empty
    value never wipes an existing one (a later run without the rating keeps the rating)."""
    path = Path(path)
    existing = {r[key]: r for r in read_rows(path)}
    known = COLUMNS.get(path.name)
    header = list(known) if known else []
    for r in list(existing.values()) + rows:
        for c in r:
            if c not in header:
                header.append(c)
    added = updated = 0
    for row in rows:
        k = _s(row.get(key))
        if not k:
            continue
        new = {c: _s(v) for c, v in row.items()}
        old = existing.get(k)
        if old is None:
            existing[k] = new
            added += 1
            continue
        merged = dict(old)
        for c, v in new.items():
            if v != "" and merged.get(c, "") != v:
                merged[c] = v
        if merged != old:
            existing[k] = merged
            updated += 1
    if added or updated or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for k in sorted(existing):
                w.writerow({c: existing[k].get(c, "") for c in header})
    return added, updated


# ── rows ─────────────────────────────────────────────────────────────────────

def _r(v, nd=2):
    if v in (None, ""):
        return ""
    x = round(float(v), nd)
    return int(x) if nd == 0 else x


def rating(summary_dto: dict | None, act: dict | None = None) -> tuple:
    """(rpe 1-10, feel word) from Garmin's post-workout rating, or ('', '') if the athlete didn't rate it."""
    src = {**(act or {}), **(summary_dto or {})}
    rpe_raw, feel_raw = src.get("directWorkoutRpe"), src.get("directWorkoutFeel")
    rpe = round(float(rpe_raw) / 10, 1) if rpe_raw not in (None, "") else ""
    feel = ""
    if feel_raw not in (None, ""):
        feel = FEEL.get(int(round(float(feel_raw) / 25)) * 25, "")
    return rpe, feel


def activity_row(act: dict, summary_dto: dict | None = None) -> dict:
    """One ledger row from a Garmin activity (list item) plus its summaryDTO (for the rating), if fetched."""
    kind = (act.get("activityType") or {}).get("typeKey", "")
    km = (act.get("distance") or 0) / 1000
    moving = act.get("movingDuration") or act.get("duration") or 0
    elapsed = act.get("duration") or moving
    hr = act.get("averageHR")
    tmin, tmax = act.get("minTemperature"), act.get("maxTemperature")
    temp = act.get("avgTemperature")
    if temp is None and tmin is not None and tmax is not None:
        temp = (tmin + tmax) / 2
    heat = heat_index_c(temp - TEMP_BIAS_C) if temp is not None and kind not in TREADMILL_KEYS else None
    gain = act.get("elevationGain")
    rpe, feel = rating(summary_dto, act)
    speed = act.get("averageSpeed") or (km * 1000 / moving if moving else None)
    return {
        "activity_id": _s(act.get("activityId")), "date": (act.get("startTimeLocal") or "")[:10],
        "start": (act.get("startTimeLocal") or "")[11:16], "type": kind, "name": (act.get("activityName") or "")[:120],
        "treadmill": int(kind in TREADMILL_KEYS), "km": _r(km), "moving_min": _r(moving / 60, 1),
        "elapsed_min": _r(elapsed / 60, 1), "avg_hr": _r(hr, 0), "max_hr": _r(act.get("maxHR"), 0),
        "avg_speed_mps": _r(speed, 3), "aerobic_te": _r(act.get("aerobicTrainingEffect"), 1),
        "anaerobic_te": _r(act.get("anaerobicTrainingEffect"), 1), "trimp": _r(trimp(elapsed / 60, hr), 1),
        "elev_gain_m": _r(gain, 0), "climb_per_km": _r(gain / km, 1) if gain is not None and km >= 0.5 else "",
        "temp_min_c": _r(tmin, 1), "temp_max_c": _r(tmax, 1), "heat_index_c": _r(heat, 1),
        "avg_cadence": _r(act.get("averageRunningCadenceInStepsPerMinute"), 0), "calories": _r(act.get("calories"), 0),
        "rpe": rpe, "feel": feel, "srpe_load": _r(rpe * moving / 60, 1) if rpe != "" else "",
    }


def plan_type(entry: dict) -> str:
    gates = [g.get("type") for g in entry.get("gates") or []]
    for t in RUN_GATES:
        if t in gates:
            return t
    if float(entry.get("run_km") or 0) > 0:
        return "run"
    return "gym" if any(t in gates for t in LIFT_GATES) or entry.get("gym") else "rest"


def deviation(planned_km, actual_km) -> str:
    """none / extended / shortened / skipped / extra, or unknown when either side is missing."""
    if planned_km in ("", None) or actual_km in ("", None):
        return "unknown"
    p, a = float(planned_km), float(actual_km)
    if p == 0 and a == 0:
        return "none"
    if p == 0:
        return "extra"
    if a == 0:
        return "skipped"
    if abs(a - p) <= max(1.0, 0.15 * p):
        return "none"
    return "extended" if a > p else "shortened"


def load_plans(root: Path = ROOT) -> dict[str, dict]:
    days = {}
    files = sorted(glob.glob(str(root / "archive" / "week_plans" / "*.py")))
    if (root / "brain_current_week_plan.py").exists():
        files.append(str(root / "brain_current_week_plan.py"))
    for f in files:
        try:
            plan = runpy.run_path(f).get("plan") or {}
        except Exception as e:
            print(f"[WARN] could not read {os.path.basename(f)}: {type(e).__name__}")
            continue
        days.update(plan.get("days") or {})
    return days


def plan_row(d: str, entry: dict, snapshot: bool) -> dict:
    """The plan for day d. A completed day's run_km holds the actual (written after the fact), so its plan is
    unknown unless a morning snapshot caught it first."""
    completed = bool(entry.get("completed")) or "COMPLETED" in f"{entry.get('label', '')} {entry.get('session', '')}"
    return {"date": d, "weekday": WEEKDAYS[date.fromisoformat(d).weekday()], "planned_type": plan_type(entry),
            "planned_km": "" if completed and not snapshot else _r(entry.get("run_km") or 0),
            "planned_label": str(entry.get("label") or "")[:120],
            "plan_source": "snapshot" if snapshot else ("file (actual)" if completed else "file")}


def run_km_by_day(activities: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for a in activities:
        if a.get("type") in RUN_TYPE_KEYS and a.get("km") not in ("", None):
            out[a["date"]] = out.get(a["date"], 0.0) + float(a["km"])
    return out


def with_actuals(rows: list[dict], km_by_day: dict[str, float], covered: set[str]) -> list[dict]:
    """Add done / actual_km / deviation for days the activity ledger covers."""
    out = []
    for r in rows:
        r = dict(r)
        if r["date"] in covered:
            actual = round(km_by_day.get(r["date"], 0.0), 2)
            r["actual_km"] = actual
            r["done"] = int(actual > 0) if r.get("planned_type") not in ("rest", "gym") else ""
            r["deviation"] = deviation(r.get("planned_km"), actual)
        out.append(r)
    return out


def existing_plan_snapshot(path: Path, d: str) -> bool:
    return any(r["date"] == d and r.get("plan_source") == "snapshot" and r.get("planned_km") != ""
               for r in read_rows(path))


# ── Garmin ───────────────────────────────────────────────────────────────────

def _call(fn, *a, **k):
    try:
        return fn(*a, **k), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:160]


def fetch_activities(client, start: date, end: date, chunk_days: int = 60) -> list[dict]:
    acts, d = [], start
    while d <= end:
        e = min(end, d + timedelta(days=chunk_days - 1))
        got, err = _call(client.get_activities_by_date, d.isoformat(), e.isoformat())
        if err:
            raise RuntimeError(f"activities {d}..{e}: {err}")
        acts.extend(got or [])
        d = e + timedelta(days=1)
    return acts


def summary_for(client, activity_id, pause: float = 0.0) -> dict | None:
    got, err = _call(client.get_activity, activity_id)
    if pause:
        time.sleep(pause)
    return (got or {}).get("summaryDTO") if not err else None


def daily_row(client, day: str, activities: list[dict]) -> dict:
    """Morning vitals of `day` (HRV last night, sleep, RHR) and that day's intake, exercise and EA."""
    from core_common import hrv_status
    row = {"date": day}
    h, err = _call(hrv_status, client, date.fromisoformat(day))
    if h:
        garmin_floor = str(h.get("floor_source", "")).startswith("Garmin")     # never store the fallback as data
        row.update(hrv=_r(h.get("last_night"), 0), hrv_7d=_r(h.get("weekly"), 0),
                   hrv_floor=_r(h.get("floor"), 0) if garmin_floor else "")
    s, err = _call(client.get_sleep_data, day)
    secs = ((s or {}).get("dailySleepDTO") or {}).get("sleepTimeSeconds")
    row["sleep_h"] = _r(secs / 3600, 2) if secs else ""
    u, err = _call(client.get_user_summary, day)
    u = u or {}
    row.update(rhr=_r(u.get("restingHeartRate"), 0), body_battery=_r(u.get("bodyBatteryMostRecentValue"), 0),
               steps=_r(u.get("totalSteps"), 0))
    eaten = u.get("consumedKilocalories")
    exercise = sum(float(a.get("calories") or 0) for a in activities if a.get("date") == day and a.get("type") != "walking")
    if eaten:
        row.update(eaten_kcal=_r(eaten, 0), exercise_kcal=_r(exercise, 0), ea=_r((eaten - exercise) / LEAN_MASS_KG, 1))
    return row


def range_daily_rows(client, start: date, end: date) -> list[dict]:
    """HRV, RHR, weight and VO2max for many days with range calls (the backfill; no per-day calls)."""
    import core_progress_tracker as pt
    daily, gaps = pt.fetch_daily_metrics(client, start, end)
    for k, why in gaps.items():
        print(f"[DATA GAP] {k}: {why}")
    days = set()
    for series in daily.values():
        days |= set(series)
    return [{"date": d, "hrv": _r(daily.get("hrv", {}).get(d), 0), "rhr": _r(daily.get("rhr", {}).get(d), 0),
             "weight_kg": _r(daily.get("weight", {}).get(d), 2), "vo2max": _r(daily.get("vo2max", {}).get(d), 1)}
            for d in sorted(days)]


# ── commands ─────────────────────────────────────────────────────────────────

def rebuild_plans(ledger_dir: Path = LEDGER, root: Path = ROOT) -> tuple[int, int]:
    """plan.csv from every week plan, with actuals where the activity ledger covers the day. Snapshot rows
    (the morning plan) are never replaced by the file's overwritten version."""
    path = ledger_dir / "plan.csv"
    acts = read_rows(ledger_dir / "activities.csv")
    covered = {r["date"] for r in read_rows(ledger_dir / "daily.csv")} | {a["date"] for a in acts}
    snaps = {r["date"] for r in read_rows(path) if r.get("plan_source") == "snapshot"}
    rows = [plan_row(d, e, snapshot=False) for d, e in sorted(load_plans(root).items())
            if re.fullmatch(r"\d{4}-\d\d-\d\d", d) and d not in snaps and d < today_pht().isoformat()]
    rows = with_actuals(rows, run_km_by_day(acts), covered)
    return upsert_csv(path, rows, "date")


def morning(client, today: date, ledger_dir: Path = LEDGER) -> None:
    """Yesterday's activities (with ratings) and daily row; today's vitals; today's plan snapshot; yesterday's actuals."""
    yday = today - timedelta(days=1)
    acts_raw = fetch_activities(client, yday, today)
    rows = []
    for a in acts_raw:
        summary = summary_for(client, a.get("activityId")) if a.get("activityId") else None
        rows.append(activity_row(a, summary))
    print("activities: added %d, updated %d" % upsert_csv(ledger_dir / "activities.csv", rows, "activity_id"))
    all_acts = read_rows(ledger_dir / "activities.csv")
    for d in (yday, today):
        print(f"daily {d}: added %d, updated %d" % upsert_csv(ledger_dir / "daily.csv", [daily_row(client, d.isoformat(), all_acts)], "date"))
    plans = load_plans()
    path = ledger_dir / "plan.csv"
    t = today.isoformat()
    if t in plans and not existing_plan_snapshot(path, t):
        upsert_csv(path, [plan_row(t, plans[t], snapshot=True)], "date")
        print(f"plan {t}: morning snapshot saved")
    yrows = [r for r in read_rows(path) if r["date"] == yday.isoformat()]
    if not yrows and yday.isoformat() in plans:
        yrows = [plan_row(yday.isoformat(), plans[yday.isoformat()], snapshot=False)]
    if yrows:
        upsert_csv(path, with_actuals(yrows, run_km_by_day(all_acts), {yday.isoformat()}), "date")


def backfill(client, since: date, today: date, ledger_dir: Path = LEDGER, pause: float = 0.4) -> None:
    acts_raw = fetch_activities(client, since, today)
    have = {r["activity_id"]: r for r in read_rows(ledger_dir / "activities.csv")}
    rows = []
    for i, a in enumerate(acts_raw, 1):
        aid = str(a.get("activityId") or "")
        kind = (a.get("activityType") or {}).get("typeKey", "")
        need_rating = kind in RUN_TYPE_KEYS or kind == "strength_training"
        summary = None
        if need_rating and aid and not (have.get(aid) or {}).get("rpe"):
            summary = summary_for(client, a.get("activityId"), pause=pause)   # one call per run / lift, paced
        rows.append(activity_row(a, summary))
        if i % 50 == 0:
            print(f"  {i}/{len(acts_raw)} activities read")
    print("activities: added %d, updated %d" % upsert_csv(ledger_dir / "activities.csv", rows, "activity_id"))
    print("daily (range: HRV, RHR, weight, VO2max): added %d, updated %d"
          % upsert_csv(ledger_dir / "daily.csv", range_daily_rows(client, since, today), "date"))
    print("plan: added %d, updated %d" % rebuild_plans(ledger_dir))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--morning", action="store_true", help="yesterday's activities + daily rows, today's plan snapshot")
    ap.add_argument("--backfill", action="store_true", help="fill the ledger from --since to today")
    ap.add_argument("--since", help="YYYY-MM-DD (backfill start)")
    ap.add_argument("--plans", action="store_true", help="rebuild plan.csv from the week plans (no Garmin)")
    ap.add_argument("--date", help="YYYY-MM-DD for --morning (default: today, PHT)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else today_pht()
    if args.plans:
        print("plan: added %d, updated %d" % rebuild_plans())
        return 0
    if not (args.morning or args.backfill):
        ap.error("choose --morning, --backfill --since YYYY-MM-DD, or --plans")
    from garmin_auth import get_client
    client = get_client()          # exits with [GARMIN AUTH ERROR] on a bad token
    if args.backfill:
        if not args.since:
            ap.error("--backfill needs --since YYYY-MM-DD")
        backfill(client, date.fromisoformat(args.since), today)
    else:
        morning(client, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
