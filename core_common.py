"""
core_common.py — shared time anchor and readiness signals
=========================================================
One place for the facts every gate and report needs, so no two scripts can
compute them differently:

  - PHT: all dates and times are Philippine Time (UTC+8). Use today_pht() /
    now_pht(), never date.today() (the cloud container runs on UTC, so before
    8 am PHT date.today() is still yesterday).
  - HRV gate, option B (brain_knowledge_base.md Rule 11).
  - Mechanical ACWR (running km) and load ACWR (Banister TRIMP of every
    activity, so bike, SkiErg and lifting count too).
  - Hours since the last hard or long run ended.
  - Readiness (TRS) and form (FBI) from the Kiat Engine.

Every function takes a garminconnect client from garmin_auth.get_client().
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, time, timedelta, timezone

PHT = timezone(timedelta(hours=8))

# Garmin typeKeys that count as a run (outdoor, treadmill, track, trail)
RUN_TYPE_KEYS = ('running', 'treadmill_running', 'trail_running', 'indoor_running', 'track_running')

ACWR_CAUTION = 1.3
ACWR_VETO = 1.5            # CLAUDE.md hard gate (the athlete, Oct 11 2026)

HRV_FALLBACK_FLOOR = 116   # used only when Garmin gives no balanced-low baseline
HRV_TREND_AVG_FLAG = 108   # Rule 11 weekly trend flag: 4-week average below this
HRV_TREND_LOW_FLAG = 103   # ... or Garmin's balanced-low below this
RHR_RISE_BPM = 5           # option B: resting HR this far above normal backs up a low HRV

# A run counts as "long" or "hard" for the hours-since rule
LONG_RUN_KM = 16.0
LONG_RUN_MIN = 90.0
HARD_AEROBIC_TE = 3.5
HARD_ANAEROBIC_TE = 2.0
# Same-day runs this close together are one session (e.g. outdoor part + treadmill part)
SPLIT_RUN_GAP_MIN = 90

HR_REST = 40
HR_MAX = 206

# Body composition: the single source for every script (brain_knowledge_base.md section 1b).
# Update here, nowhere else. Set your own lean mass and bodyweight.
LEAN_MASS_KG = 60.0        # Example lean body mass (kg); set your own
BODY_WEIGHT_KG = 70.0      # Example bodyweight (kg); set your own
EA_FLOOR, EA_TARGET = 30, 45   # energy availability, kcal per kg lean mass (Rule 7, P30)


def kcal_to_reach_ea(eaten_kcal: float, exercise_kcal: float, target: float, lean_kg: float = LEAN_MASS_KG) -> int:
    """Kcal still to eat today to reach an energy availability `target` (0 if already there).
    EA = (eaten - exercise) / lean mass, so the gap is target * lean + exercise - eaten."""
    return max(0, round(target * lean_kg + exercise_kcal - eaten_kcal))


def ea_gap_line(eaten_kcal: float, exercise_kcal: float) -> str | None:
    """One line, e.g. 'To reach EA 30 (floor): +150 kcal; 45 (target): +1,060 kcal', or None at 45+."""
    to_target = kcal_to_reach_ea(eaten_kcal, exercise_kcal, EA_TARGET)
    if not to_target:
        return None
    to_floor = kcal_to_reach_ea(eaten_kcal, exercise_kcal, EA_FLOOR)
    floor = f"EA {EA_FLOOR} (floor): +{to_floor:,} kcal; " if to_floor else ""
    return f"To reach {floor}EA {EA_TARGET} (target): +{to_target:,} kcal (carbs and protein, not a snack)."


def now_pht() -> datetime:
    return datetime.now(PHT)


def today_pht() -> date:
    return now_pht().date()


def parse_local(ts: str) -> datetime:
    """Garmin startTimeLocal ('YYYY-MM-DD HH:MM:SS') as an aware PHT datetime."""
    return datetime.fromisoformat(ts[:19].replace(' ', 'T')).replace(tzinfo=PHT)


def run_km(act: dict) -> float:
    return (act.get('distance') or 0) / 1000.0


def is_run(act: dict) -> bool:
    return act.get('activityType', {}).get('typeKey') in RUN_TYPE_KEYS


def act_end(act: dict) -> datetime:
    """When an activity ended (PHT); a merged run carries its own '_end'."""
    return act.get('_end') or parse_local(act['startTimeLocal']) + timedelta(seconds=act.get('duration') or 0)


def merge_split_runs(acts: list[dict], gap_min: float = SPLIT_RUN_GAP_MIN) -> list[dict]:
    """Same-day runs that start within `gap_min` minutes of the previous one ending are
    merged into one session (distance and time summed, HR time-weighted, highest TE).
    Other activities pass through unchanged. A merged run carries '_merge_note' (one line
    to print so the athlete can check the merge) and '_parts'."""
    runs = sorted((a for a in acts if is_run(a) and a.get('startTimeLocal')), key=lambda a: a['startTimeLocal'])
    out = [a for a in acts if not (is_run(a) and a.get('startTimeLocal'))]
    group: list[dict] = []

    def flush():
        if len(group) == 1:
            out.append(group[0])
        elif group:
            out.append(_merge(group))

    for a in runs:
        if group and a['startTimeLocal'][:10] == group[-1]['startTimeLocal'][:10] and \
                (parse_local(a['startTimeLocal']) - act_end(group[-1])).total_seconds() <= gap_min * 60:
            group.append(a)
        else:
            flush()
            group = [a]
    flush()
    return out


def _merge(parts: list[dict]) -> dict:
    m = dict(parts[0])
    secs = sum(p.get('duration') or 0 for p in parts)
    hr_secs = [(p.get('averageHR'), p.get('duration') or 0) for p in parts if p.get('averageHR')]
    m['distance'] = sum(p.get('distance') or 0 for p in parts)
    m['duration'] = secs
    m['movingDuration'] = sum(p.get('movingDuration') or p.get('duration') or 0 for p in parts)
    m['averageHR'] = (sum(h * d for h, d in hr_secs) / sum(d for _, d in hr_secs)) if hr_secs and sum(d for _, d in hr_secs) else None
    m['maxHR'] = max((p.get('maxHR') or 0 for p in parts), default=None) or None
    for k in ('aerobicTrainingEffect', 'anaerobicTrainingEffect'):
        m[k] = max((p.get(k) or 0 for p in parts), default=0) or None
    for z in range(1, 6):
        m[f'hrTimeInZone_{z}'] = sum(p.get(f'hrTimeInZone_{z}') or 0 for p in parts)
    for k in ('elevationGain', 'elevationLoss', 'calories'):
        m[k] = sum(p.get(k) or 0 for p in parts)
    m['_mixed'] = len({p.get('activityType', {}).get('typeKey') for p in parts}) > 1
    m['activityName'] = ' + '.join(p.get('activityName') or '?' for p in parts)
    m['_end'] = act_end(parts[-1])
    m['_parts'] = parts
    gaps = [round((parse_local(b['startTimeLocal']) - act_end(a)).total_seconds() / 60) for a, b in zip(parts, parts[1:])]
    m['_merge_note'] = (f"Merged split run {parts[0]['startTimeLocal'][:10]}: " +
                        " + ".join(f"{run_km(p):.2f} km ({p.get('activityName') or '?'})" for p in parts) +
                        f" = {run_km(m):.2f} km; gap {', '.join(f'{g} min' for g in gaps)} "
                        f"(rule: same day, gap <= {SPLIT_RUN_GAP_MIN} min)")
    return m


def merge_notes(acts: list[dict]) -> list[str]:
    return [a['_merge_note'] for a in acts if a.get('_merge_note')]


# ── HRV, option B ────────────────────────────────────────────────────────────

def hrv_status(client, day: date) -> dict:
    """HRV gate for the morning of `day` (option B, Rule 11).

    trips    : Z4/5 blocked: last night below the floor AND (7-day average below
               the floor OR resting HR >= normal + 5 bpm)
    low_night: last night below the floor (on its own only a warning)
    trend    : Rule 11 weekly trend flag (warning)
    missing  : no overnight reading (watch not worn or not synced)
    """
    out = {"day": day.isoformat(), "missing": True, "trips": False, "low_night": False,
           "trend": False, "notes": []}
    try:
        start = day - timedelta(days=27)
        rng = client.get_hrv_data_range(start.isoformat(), day.isoformat()) or {}
        summaries = {s.get('calendarDate'): s for s in rng.get('hrvSummaries') or []}
    except Exception as e:
        out["notes"].append(f"HRV fetch failed: {e}")
        return out

    s = summaries.get(day.isoformat()) or {}
    last = s.get('lastNightAvg')
    weekly = s.get('weeklyAvg')
    base_low = (s.get('baseline') or {}).get('balancedLow')
    floor = float(base_low) if base_low else float(HRV_FALLBACK_FLOOR)
    nights = [v.get('lastNightAvg') for v in summaries.values() if v.get('lastNightAvg')]
    avg_4wk = sum(nights) / len(nights) if nights else None

    rhr_today = rhr_normal = None
    try:
        rhr = client.get_rhr_daily((day - timedelta(days=28)).isoformat(), day.isoformat()) or []
        vals = {r.get('calendarDate'): r.get('value') for r in rhr if r.get('value')}
        rhr_today = vals.get(day.isoformat())
        prior = [v for d, v in vals.items() if d != day.isoformat()]
        rhr_normal = statistics.median(prior) if prior else None
    except Exception as e:
        out["notes"].append(f"RHR fetch failed: {e}")

    out.update({"last_night": last, "weekly": weekly, "floor": floor,
                "floor_source": "Garmin balanced-low" if base_low else "fallback 116",
                "avg_4wk": round(avg_4wk, 1) if avg_4wk else None,
                "rhr_today": rhr_today, "rhr_normal": rhr_normal})
    if last is None:
        return out
    out["missing"] = False
    out["low_night"] = last < floor
    weekly_low = weekly is not None and weekly < floor
    rhr_up = rhr_today is not None and rhr_normal is not None and rhr_today >= rhr_normal + RHR_RISE_BPM
    out["weekly_low"] = weekly_low
    out["rhr_up"] = rhr_up
    out["trips"] = out["low_night"] and (weekly_low or rhr_up)
    out["trend"] = (avg_4wk is not None and avg_4wk < HRV_TREND_AVG_FLAG) or \
                   (base_low is not None and base_low < HRV_TREND_LOW_FLAG)
    return out


def describe_hrv(h: dict) -> str:
    if h["missing"]:
        return f"No overnight HRV for {h['day']} (watch not worn or not synced)."
    bits = [f"last night {h['last_night']} ms vs floor {h['floor']:.0f} ({h['floor_source']})",
            f"7-day avg {h['weekly']}", f"RHR {h['rhr_today']} vs normal {h['rhr_normal']}"]
    if h["trips"]:
        verdict = "TRIPS option B (no Zone 4/5)"
    elif h["low_night"]:
        verdict = "single low night: warning only"
    else:
        verdict = "OK"
    return f"{verdict} | " + ", ".join(bits)


# ── ACWR ────────────────────────────────────────────────────────────────────

def trimp(duration_min: float, avg_hr: float | None) -> float:
    """Banister TRIMP (male weighting)."""
    if not avg_hr or duration_min <= 0:
        return 0.0
    r = max(0.0, min(1.0, (avg_hr - HR_REST) / (HR_MAX - HR_REST)))
    return duration_min * r * 0.64 * math.exp(1.92 * r)


def activities_window(client, end: date, days: int = 28) -> list[dict]:
    start = end - timedelta(days=days - 1)
    return client.get_activities_by_date(start.isoformat(), end.isoformat()) or []


def acwr(client, end: date, extra_km: float = 0.0, extra_trimp: float = 0.0,
         acts: list[dict] | None = None) -> dict:
    """Mechanical (run km) and load (TRIMP, all activities) ACWR for the 7 days
    ending `end` vs. the 28 days ending `end`. extra_* adds a proposed session."""
    acts = acts if acts is not None else activities_window(client, end)
    acute_start = end - timedelta(days=6)
    km7 = km28 = t7 = t28 = 0.0
    for a in acts:
        d = (a.get('startTimeLocal') or '')[:10]
        if not d:
            continue
        d = date.fromisoformat(d)
        if d > end or d < end - timedelta(days=27):
            continue
        t = trimp((a.get('duration') or 0) / 60.0, a.get('averageHR'))
        t28 += t
        if d >= acute_start:
            t7 += t
        if is_run(a):
            km28 += run_km(a)
            if d >= acute_start:
                km7 += run_km(a)

    def ratio(acute, total, extra):
        chronic_week = (total + extra) / 4.0
        return round((acute + extra) / chronic_week, 3) if chronic_week > 0 else 0.0

    mech, load = ratio(km7, km28, extra_km), ratio(t7, t28, extra_trimp)
    return {"mechanical": mech, "load": load, "gate": max(mech, load),
            "acute_km": round(km7 + extra_km, 1), "chronic_km_week": round((km28 + extra_km) / 4, 1),
            "acute_trimp": round(t7 + extra_trimp), "chronic_trimp_week": round((t28 + extra_trimp) / 4)}


# ── Hours since the last hard or long run ────────────────────────────────────

def is_hard_or_long(act: dict) -> str | None:
    if not is_run(act):
        return None
    km = run_km(act)
    mins = (act.get('duration') or 0) / 60.0
    if km >= LONG_RUN_KM or mins >= LONG_RUN_MIN:
        return "long"
    if (act.get('aerobicTrainingEffect') or 0) >= HARD_AEROBIC_TE or \
       (act.get('anaerobicTrainingEffect') or 0) >= HARD_ANAEROBIC_TE:
        return "hard"
    return None


def last_hard_or_long(client, at: datetime, acts: list[dict] | None = None) -> dict | None:
    """The most recent hard or long run that ended before `at`, with hours elapsed."""
    acts = acts if acts is not None else activities_window(client, at.date(), days=10)
    best = None
    for a in merge_split_runs(acts):
        kind = is_hard_or_long(a)
        if not kind or not a.get('startTimeLocal'):
            continue
        end = act_end(a)
        if end > at:
            continue
        if best is None or end > best["end"]:
            best = {"end": end, "kind": kind, "km": round(run_km(a), 1),
                    "te": round(a["aerobicTrainingEffect"], 1) if a.get("aerobicTrainingEffect") else None,
                    "name": a.get("activityName"),
                    "merge_note": a.get('_merge_note')}
    if best:
        best["hours"] = round((at - best["end"]).total_seconds() / 3600.0, 1)
    return best


# ── Readiness (TRS) and form (FBI) from the Kiat Engine ─────────────────────

def readiness_reference(day: date) -> datetime:
    """The moment a readiness score is for: now for today, 06:00 PHT on any other day."""
    now = now_pht()
    return now if day == now.date() else datetime.combine(day, time(6, 0), tzinfo=PHT)


def readiness(client, day: date, acts: list[dict] | None = None) -> dict:
    """TRS for the morning of `day` (now, if `day` is today) and FBI of the last run before it.
    Recovery hours and the ACWR window are measured from that moment, not from the wall clock,
    and activities that started after it are ignored."""
    from core_physiological_engine import (PhysiologicalEngine, parse_sleep_data,
                                           parse_hrv_data, parse_activity_window, parse_laps)
    out = {"trs": None, "fbi": None, "notes": []}
    ref = readiness_reference(day)
    try:
        acts = acts if acts is not None else activities_window(client, day, days=29)
        acts = sorted((a for a in acts if a.get('startTimeLocal') and parse_local(a['startTimeLocal']) <= ref),
                      key=lambda a: a['startTimeLocal'], reverse=True)
        sleep = (client.get_sleep_data(day.isoformat()) or {}).get('dailySleepDTO', {})
        hrv = (client.get_hrv_data(day.isoformat()) or {}).get('hrvSummary', {})
        summary = client.get_user_summary(day.isoformat()) or {}
        runs = [a for a in acts if is_run(a)]
        laps, rec_hours, end_time = [], None, None
        if runs:
            last = runs[0]
            rec_hours = float(last['recoveryTime']) if last.get('recoveryTime') else None
            end_time = parse_local(last['startTimeLocal']) + timedelta(seconds=last.get('duration') or 0)
        # FBI only from an outdoor run: the Forerunner 165 misreads treadmill cadence
        # and stride (it estimates them from the wrist), which fakes a form collapse.
        outdoor = [a for a in runs if a.get('activityType', {}).get('typeKey') == 'running']
        if outdoor:
            out["fbi_run"] = f"{outdoor[0].get('startTimeLocal', '')[:10]} {run_km(outdoor[0]):.1f} km"
            try:
                splits = client.get_activity_splits(outdoor[0]['activityId']) or {}
                laps = parse_laps(splits.get('lapDTOs') or [])
            except Exception as e:
                out["notes"].append(f"lap fetch failed: {e}")
        stress = summary.get('averageStressLevel')
        report = PhysiologicalEngine().compute(
            sleep=parse_sleep_data(sleep), hrv=parse_hrv_data(hrv), recovery_hours=rec_hours,
            activity_end_time_utc=end_time, rolling_stress_3d=float(stress) if stress else None,
            activity_window=parse_activity_window(acts), laps=laps or None,
            now_utc=ref.astimezone(timezone.utc))
        out["trs"] = report.trs_score
        out["fbi"] = report.fbi_score
    except Exception as e:
        out["notes"].append(f"readiness failed: {e}")
    return out


# ── Decision log (consults and the athlete's overrides) ──────────────────────────────

DECISION_LOG = "brain_decision_log.md"


def log_decision(session: str, day: str | None, gate: str, decision: str, outcome: str = "") -> None:
    """Append a row to brain_decision_log.md: what the gate said and what the athlete decided.
    Fill in the outcome column after the session to learn whose call was right."""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DECISION_LOG)
    row = (f"| {now_pht():%Y-%m-%d %H:%M} | {day or ''} | {session} | {gate} | "
           f"{decision.replace('|', '/')} | {outcome} |\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(row)
