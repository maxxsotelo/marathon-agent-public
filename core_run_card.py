"""
core_run_card.py — the run card: every run's key telemetry in one compact block
===============================================================================
The card is the part of the run analysis that is always shown to the athlete IN FULL, by
any AI or by hand (CLAUDE.md, Rule 6). It is deterministic: the same run gives the
same card whatever model reads it. Sections:

  1. Summary       distance, time, pace, grade-adjusted pace, HR, training effect
  2. Conditions    wrist temperature, estimated air temperature, heat index, heat cost in bpm
  3. Terrain       climb and descent, elevation range, metres climbed per km, rating
  4. Km splits     pace, GAP, HR, cadence, climb/descent, temperature for every km
  5. Fast segments strides, sprints and work reps: distance, time, average and peak
                   speed, slope, HR, cadence, ground contact, stride, power
  6. Efficiency    metres per minute per heartbeat, against the last 90 days of
                   comparable outdoor easy runs; HR/pace decoupling
  7. Treadmill     note when the watch's speed and wrist HR are unreliable

Usage:
    python core_run_card.py                  # yesterday's longest run (PHT)
    python core_run_card.py 2026-10-16       # longest run that day
    python core_run_card.py 2026-10-16 ACTIVITY_ID
core_run_analysis.py prints this card at the end of its report run.
"""

import os
import statistics as st
import sys
from datetime import date, datetime, timedelta, timezone

from core_common import RUN_TYPE_KEYS, today_pht

TEMP_BIAS_C = 4.0        # FR 165 wrist sensor reads ~3-5 C above the air (core_run_analysis.py)
DEFAULT_RH = 70.0        # assumed humidity (the watch has no humidity sensor)
FAST_FACTOR = 1.20       # a lap is a "fast segment" when >= 20% quicker than the steady pace
EASY_HR = (130, 155)     # comparison window for the efficiency trend (outdoor easy runs)


# ── helpers ─────────────────────────────────────────────────────────────────
def pace_str(speed_mps):
    if not speed_mps:
        return "-"
    s = round(1000 / speed_mps)
    return f"{s // 60}:{s % 60:02d}"


def kph(speed_mps):
    return f"{speed_mps * 3.6:.1f}" if speed_mps else "-"


def heat_index_c(temp_c, rh=DEFAULT_RH):
    t = temp_c * 9 / 5 + 32
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 0.00683783 * t * t
          - 0.05481717 * rh * rh + 0.00122874 * t * t * rh + 0.00085282 * t * rh * rh
          - 0.00000199 * t * t * rh * rh)
    return (hi - 32) * 5 / 9 if temp_c >= 26.7 else temp_c   # Rothfusz is valid above ~80 F


def heat_bpm(hi_c):
    extra = max(0.0, hi_c - 28)
    return int(extra * 1.5), int(extra * 2.5)


def terrain_label(m_per_km):
    if m_per_km < 5:
        return "flat"
    if m_per_km < 10:
        return "rolling"
    return "hilly"


def gmt_ms(ts):
    """Lap startTimeGMT '2026-10-16T09:03:10.0' -> epoch ms."""
    return int(datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=timezone.utc).timestamp() * 1000)


def ef(speed_mps, hr):
    """Efficiency: metres per minute per heartbeat (higher is better)."""
    return speed_mps * 60 / hr if speed_mps and hr else None


# ── data ────────────────────────────────────────────────────────────────────
def load(client, day, activity_id=None):
    acts = client.get_activities_by_date(day, day)
    runs = [a for a in acts if a.get("activityType", {}).get("typeKey") in RUN_TYPE_KEYS]
    if activity_id:
        runs = [a for a in runs if str(a["activityId"]) == str(activity_id)]
    if not runs:
        return None
    run = max(runs, key=lambda a: a.get("distance") or 0)
    det = client.get_activity_details(run["activityId"], maxchart=10000, maxpoly=0)
    idx = {m["key"]: m["metricsIndex"] for m in det.get("metricDescriptors", [])}

    def col(row, key):
        i = idx.get(key)
        return row["metrics"][i] if i is not None else None

    pts = []
    for row in det.get("activityDetailMetrics", []):
        p = {k: col(row, src) for k, src in (
            ("t", "directTimestamp"), ("mt", "sumMovingDuration"), ("d", "sumDistance"), ("v", "directSpeed"), ("gap", "directGradeAdjustedSpeed"),
            ("hr", "directHeartRate"), ("ele", "directElevation"), ("temp", "directAirTemperature"),
            ("cad", "directDoubleCadence"), ("gct", "directGroundContactTime"), ("stride", "directStrideLength"),
            ("w", "directPower"))}
        if p["t"] is not None and p["d"] is not None:
            pts.append(p)
    laps = client.get_activity_splits(run["activityId"]).get("lapDTOs", [])
    return run, pts, laps


def stats(pts, key, fn):
    vals = [p[key] for p in pts if p.get(key) is not None]
    return fn(vals) if vals else None


def climb(pts, window=9):
    """Climb and descent from the elevation stream, smoothed (moving average) so GPS/barometer
    jitter of a few decimetres per second does not add up to fake metres."""
    ele = [p["ele"] for p in pts if p.get("ele") is not None]
    if len(ele) > window:
        h = window // 2
        ele = [st.mean(ele[max(0, i - h):i + h + 1]) for i in range(len(ele))]
    up = down = 0.0
    anchor = ele[0] if ele else 0
    for e in ele[1:]:                      # count a change only once it reaches 1 m (hysteresis)
        if e - anchor >= 1:
            up, anchor = up + e - anchor, e
        elif anchor - e >= 1:
            down, anchor = down + anchor - e, e
    return up, down, ele


def history_ef(client, before: str, days=90):
    start = (date.fromisoformat(before) - timedelta(days=days)).isoformat()
    acts = client.get_activities_by_date(start, (date.fromisoformat(before) - timedelta(days=1)).isoformat())
    out = []
    for a in acts:
        if a.get("activityType", {}).get("typeKey") != "running":
            continue
        dist, dur, hr = a.get("distance") or 0, a.get("movingDuration") or a.get("duration") or 0, a.get("averageHR")
        if 4000 <= dist <= 13000 and hr and EASY_HR[0] <= hr <= EASY_HR[1] and dur:
            out.append(ef(dist / dur, hr))
    return out


# ── card ────────────────────────────────────────────────────────────────────
FEEL_WORDS = {0: "very weak", 25: "weak", 50: "normal", 75: "strong", 100: "very strong"}


def workout_rating(summary):
    """the athlete's own rating entered on the watch after the run (Garmin summaryDTO: directWorkoutRpe 10-100,
    directWorkoutFeel 0/25/50/75/100), as text; None when he entered neither."""
    summary = summary or {}
    rpe, feel = summary.get("directWorkoutRpe"), summary.get("directWorkoutFeel")
    parts = []
    if rpe is not None:
        parts.append(f"effort {round(float(rpe) / 10)}/10")
    if feel is not None:
        parts.append(f"feel {FEEL_WORDS.get(int(round(float(feel) / 25)) * 25, str(feel))}")
    return " | ".join(parts) or None


def rating_line(client, activity_id):
    """The run card line for the athlete's watch rating, or None if Garmin can't be asked (never a guess)."""
    try:
        summary = (client.get_activity(activity_id) or {}).get("summaryDTO")
    except Exception:
        return None
    rating = workout_rating(summary)
    return f"- Your rating on the watch: {rating}" if rating else "- Your rating on the watch: not entered (perceived effort and feel)"


def model_line(run):
    """'x % vs expected for these conditions' from the athlete's learned efficiency model (build_plan T21,
    scripts/learn_weekly.py), for steady outdoor runs: the runs the model learns from. None when there is no
    fresh model (data/models/efficiency.json, retrained on Sundays) or the run isn't comparable."""
    scripts = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import efficiency_model as em
        from ledger import activity_row
        from model_utils import steady_runs
    except ImportError:
        return None
    model = em.load()
    rows = steady_runs([activity_row(run)]) if model else []
    if not rows or rows[0]["heat_index_c"] is None or rows[0]["climb_per_km"] is None:
        return None
    r = rows[0]
    exp = em.expected_ef(model, r["heat_index_c"], r["climb_per_km"], r["date"])
    pct = em.residual_pct(model, r)
    return (f"- Vs your learned model ({model['n']} steady runs, retrained {model['generated']}): for a "
            f"{r['heat_index_c']:.0f} C heat index and {r['climb_per_km']:.0f} m/km it expects {exp:.2f}; whole run "
            f"{r['ef']:.2f} -> {pct:+.1f} % vs expected for these conditions (typical scatter +/-"
            f"{model['resid_sd'] / exp * 100:.1f} %)")


def build_card(client, day=None, activity_id=None):
    day = day or (today_pht() - timedelta(days=1)).isoformat()
    loaded = load(client, day, activity_id)
    if not loaded:
        return f"No running activity found on {day}."
    run, pts, laps = loaded
    kind = run["activityType"]["typeKey"]
    treadmill = kind in ("treadmill_running", "indoor_running")
    dist = (run.get("distance") or 0)
    moving = run.get("movingDuration") or run.get("duration") or 0
    L = [f"## Run card: {run.get('activityName', '')} ({run['startTimeLocal'][:16]}, {kind})", ""]

    # 1. Summary
    gap = stats(pts, "gap", st.mean)
    L += ["### Summary",
          f"- {dist / 1000:.2f} km in {int(moving // 60)}:{int(moving % 60):02d} moving | pace {pace_str(dist / moving if moving else 0)}/km"
          + (f" | grade-adjusted {pace_str(gap)}/km" if gap and not treadmill else ""),
          f"- HR avg {run.get('averageHR', 0):.0f} / max {run.get('maxHR', 0):.0f} | training effect "
          f"{run.get('aerobicTrainingEffect') or 0:.1f} aerobic, {run.get('anaerobicTrainingEffect') or 0:.1f} anaerobic"
          f" | max speed {kph(run.get('maxSpeed'))} kph | max cadence {run.get('maxRunningCadenceInStepsPerMinute') or 0:.0f} spm"]
    rating = rating_line(client, run["activityId"])
    L += ([rating] if rating else []) + [""]

    # 2. Conditions
    tmin, tmax = stats(pts, "temp", min), stats(pts, "temp", max)
    L.append("### Conditions")
    if tmin is not None:
        amin, amax = tmin - TEMP_BIAS_C, tmax - TEMP_BIAS_C
        hmin, hmax = heat_index_c(amin), heat_index_c(amax)
        lo, hi = heat_bpm(hmax)
        L.append(f"- Wrist sensor {tmin:.0f}-{tmax:.0f} C -> air ~{amin:.0f}-{amax:.0f} C | heat index ~{hmin:.0f}-{hmax:.0f} C "
                 f"(at {DEFAULT_RH:.0f}% RH assumed) | heat cost ~+{lo}-{hi} bpm")
    else:
        L.append("- No temperature recorded.")
    L.append("")

    # 3. Terrain
    up, down, ele = climb(pts)
    L.append("### Terrain")
    if treadmill or not ele:
        L.append("- Treadmill / no elevation data (set incline is not recorded by the watch).")
    else:
        if run.get("elevationGain") is not None:
            up, down = run["elevationGain"], run.get("elevationLoss") or down   # Garmin's own totals
        mpk = up / (dist / 1000) if dist else 0
        L.append(f"- +{up:.0f} m / -{down:.0f} m | elevation {min(ele):.0f}-{max(ele):.0f} m | {mpk:.1f} m climbed per km: "
                 f"{terrain_label(mpk)}")
    L.append("")

    # 4. Km splits
    L += ["### Km splits", "| km | pace | GAP | HR avg/max | cadence | climb | temp |", "|---|---|---|---|---|---|---|"]
    k, seg = 1, []
    for p in pts:
        seg.append(p)
        if p["d"] >= k * 1000 or p is pts[-1]:
            if len(seg) > 1:
                dd = seg[-1]["d"] - seg[0]["d"]
                dt = ((seg[-1]["mt"] - seg[0]["mt"]) if seg[-1].get("mt") is not None and seg[0].get("mt") is not None
                      else (seg[-1]["t"] - seg[0]["t"]) / 1000)
                if dd >= 300:
                    u, dn, _ = climb(seg)
                    g = stats(seg, "gap", st.mean)
                    label = f"{k}" if dd >= 950 else f"{k} ({dd / 1000:.2f})"
                    L.append(f"| {label} | {pace_str(dd / dt if dt else 0)} | {pace_str(g) if g and not treadmill else '-'} | "
                             f"{stats(seg, 'hr', st.mean) or 0:.0f}/{stats(seg, 'hr', max) or 0:.0f} | "
                             f"{stats(seg, 'cad', st.mean) or 0:.0f} | {'-' if treadmill else f'+{u:.0f}/-{dn:.0f} m'} | "
                             f"{stats(seg, 'temp', st.mean) or 0:.0f} C |")
            k, seg = k + 1, [p]
    L.append("")

    # 5. Fast segments (strides, sprints, reps)
    steady = [l for l in laps if (l.get("distance") or 0) >= 500 and l.get("averageSpeed")]
    base = st.median([l["averageSpeed"] for l in steady]) if steady else stats(pts, "v", st.median)
    fast = [l for l in laps if l.get("averageSpeed") and base and l["averageSpeed"] >= FAST_FACTOR * base
            and (l.get("distance") or 0) < 2000]
    if fast:
        L += ["### Fast segments (GPS check: a speed over 18 kph needs cadence of about 185+ spm to be real)", "| # | dist / time | avg | peak | slope | HR start->end (max) | cadence max | GCT min | stride max | power max | check |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for n, lap in enumerate(fast, 1):
            t0 = gmt_ms(lap["startTimeGMT"])
            t1 = t0 + lap["duration"] * 1000
            s = [p for p in pts if t0 <= p["t"] <= t1]
            if not s:
                continue
            u, dn, e = climb(s)
            raw = [p["ele"] for p in s if p.get("ele") is not None]          # net slope from the raw stream
            slope = (raw[-1] - raw[0]) / lap["distance"] * 100 if raw and lap.get("distance") else 0
            hrs = [int(p["hr"]) for p in s if p.get("hr")]
            peak = max(stats(s, "v", max) or 0, lap.get("maxSpeed") or 0, lap["averageSpeed"])
            fmt = lambda v, unit="": "-" if v is None else f"{v:.0f}{unit}"
            L.append(f"| {n} | {lap['distance']:.0f} m / {lap['duration']:.0f} s | {kph(lap['averageSpeed'])} kph "
                     f"({pace_str(lap['averageSpeed'])}) | {kph(peak)} kph | "
                     f"{'-' if treadmill else f'{slope:+.1f}% (+{u:.0f}/-{dn:.0f} m)'} | "
                     f"{hrs[0] if hrs else '-'}->{hrs[-1] if hrs else '-'} ({max(hrs) if hrs else '-'}) | "
                     f"{fmt(stats(s, 'cad', max))} spm | {fmt(stats(s, 'gct', min), ' ms')} | "
                     f"{fmt(stats(s, 'stride', max), ' cm')} | {fmt(stats(s, 'w', max), ' W')} |"
                     + (" ok |" if treadmill or lap["averageSpeed"] * 3.6 < 18 or (stats(s, "cad", max) or 0) >= 185
                        else " GPS? cadence too low for this speed |"))
        L.append("")

    # 6. Efficiency
    L.append("### Efficiency")
    first_fast = gmt_ms(fast[0]["startTimeGMT"]) if fast else None
    easy = [p for p in pts if p.get("v") and p.get("hr") and (not first_fast or p["t"] < first_fast)]
    if len(easy) < 0.5 * len(pts):      # reps throughout: use everything below the fast threshold
        easy = [p for p in pts if p.get("v") and p.get("hr") and (not base or p["v"] < FAST_FACTOR * base)]
    if easy and not treadmill:
        sp = st.mean(p["v"] for p in easy)
        hr = st.mean(p["hr"] for p in easy)
        today_ef = ef(sp, hr)
        hist = history_ef(client, day)
        line = f"- Steady running: {pace_str(sp)}/km at HR {hr:.0f} -> {today_ef:.2f} m/min per beat"
        if hist:
            med = st.median(hist)
            line += f" | 90-day median of {len(hist)} comparable outdoor easy runs: {med:.2f} ({(today_ef / med - 1) * 100:+.0f}%)"
        L.append(line)
        half = len(easy) // 2
        e1 = ef(st.mean(p["v"] for p in easy[:half]), st.mean(p["hr"] for p in easy[:half]))
        e2 = ef(st.mean(p["v"] for p in easy[half:]), st.mean(p["hr"] for p in easy[half:]))
        if e1 and e2:
            dec = (e1 - e2) / e1 * 100
            L.append(f"- HR/pace decoupling {dec:.1f}% ({'coupled' if dec < 5 else 'drifting'}: under 5% = solid aerobic run)")
        learned = model_line(run)
        L.append(learned or "- Heat and hills both lower this number: compare like with like.")
    else:
        L.append("- Treadmill: watch speed and wrist HR are unreliable indoors; judge by belt speed and effort.")
    return "\n".join(L)


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    aid = sys.argv[2] if len(sys.argv) > 2 else None
    from garmin_auth import get_client
    card = build_card(get_client(), day, aid)
    os.makedirs("scratch", exist_ok=True)
    print(card)


if __name__ == "__main__":
    main()
