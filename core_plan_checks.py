"""
core_plan_checks.py — research checks for weekly planning
=========================================================
Run when planning a week (and in the morning routine on Sundays). It measures the last
weeks of training against the principles in knowledge_base/PRINCIPLES.md, so the plan
is checked by numbers, not memory:

  P1  80/20: share of running time at or above 176 bpm (Garmin running zone 3+,
      ~ KB Zone 3, 175+) over the last 4 weeks. Target <= ~20%.
  P5  weekly km growth vs. baseline (12-week median): below baseline up to +30%/week,
      above baseline +5-10% held 2-3 weeks.
  P6  weeks since the last recovery week (<= 75% of the 3 weeks before). Flag at 4+.
  P7  volume and intensity both up in the same week.
  P8  single-run spike: a proposed long run vs. the longest run of the previous 30 days
      (Frandsen 2025: > 10% raises injury risk), shown next to the lifetime longest.
  P9  long-run share of weekly km (<= ~35%).
  P15 hard sessions per week (1-2).

Usage:
    python core_plan_checks.py [--long-run KM] [--week-km KM] [--hard N]
      --long-run  proposed long run for the coming week
      --week-km   proposed weekly km
      --hard      proposed number of hard sessions
"""

import argparse
import statistics
from collections import defaultdict
from datetime import date, timedelta

from core_common import is_hard_or_long, is_run, run_km, today_pht
from garmin_auth import get_client

HARD_HR_FLOOR_ZONE = 3          # Garmin running zone 3 starts at 176 bpm (Oct 2026)
EIGHTY_TWENTY_MAX = 0.20
LONG_RUN_SHARE_MAX = 0.35


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--long-run", type=float, help="proposed long run (km)")
    ap.add_argument("--week-km", type=float, help="proposed weekly km")
    ap.add_argument("--hard", type=int, help="proposed hard sessions next week")
    args = ap.parse_args()

    today = today_pht()
    this_monday = monday(today)
    start = this_monday - timedelta(weeks=12)
    client = get_client()
    acts = client.get_activities_by_date(start.isoformat(), today.isoformat()) or []
    runs = [a for a in acts if is_run(a)]

    weeks = defaultdict(lambda: {"km": 0.0, "long": 0.0, "hard": 0, "secs": 0.0, "hard_secs": 0.0})
    for a in runs:
        d = date.fromisoformat(a["startTimeLocal"][:10])
        w = weeks[monday(d)]
        km = run_km(a)
        w["km"] += km
        w["long"] = max(w["long"], km)
        w["secs"] += a.get("duration") or 0
        w["hard_secs"] += sum(a.get(f"hrTimeInZone_{z}") or 0 for z in range(HARD_HR_FLOOR_ZONE, 6))
        if is_hard_or_long(a) == "hard":
            w["hard"] += 1

    # Completed weeks; on Sunday (planning day) the current week counts as complete.
    end_monday = this_monday + timedelta(weeks=1) if today.weekday() == 6 else this_monday
    full_weeks = [end_monday - timedelta(weeks=i) for i in range(12, 0, -1)]
    print(f"=== PLANNING CHECKS ({today}) ===")
    print("Week of      km   long  hard  time>=176bpm")
    for m in full_weeks[-6:]:
        w = weeks[m]
        share = w["hard_secs"] / w["secs"] if w["secs"] else 0
        print(f"{m}  {w['km']:5.1f}  {w['long']:5.1f}  {w['hard']:4d}  {share:6.0%}")
    if end_monday == this_monday and weeks[this_monday]["km"]:
        w = weeks[this_monday]
        print(f"{this_monday}  {w['km']:5.1f}  {w['long']:5.1f}  {w['hard']:4d}  (this week, so far)")

    findings = []
    kms = [weeks[m]["km"] for m in full_weeks]
    baseline = statistics.median(kms) if kms else 0
    last, prev = weeks[full_weeks[-1]], weeks[full_weeks[-2]]

    # P1: 80/20 over the last 4 completed weeks
    secs = sum(weeks[m]["secs"] for m in full_weeks[-4:])
    hard_secs = sum(weeks[m]["hard_secs"] for m in full_weeks[-4:])
    share = hard_secs / secs if secs else 0
    findings.append(("WARN" if share > EIGHTY_TWENTY_MAX else "OK", "P1",
                     f"{share:.0%} of running time at 176+ bpm over 4 weeks (target <= 20%)."))

    # P5: growth vs. baseline
    findings.append(("INFO", "P5", f"Baseline (12-week median) {baseline:.1f} km/week; last week {last['km']:.1f}."))
    if args.week_km:
        growth = (args.week_km - last["km"]) / last["km"] if last["km"] else 0
        limit = 0.30 if args.week_km <= baseline else 0.10
        lvl = "WARN" if growth > limit else "OK"
        findings.append((lvl, "P5", f"Proposed {args.week_km:.0f} km = {growth:+.0%} vs last week "
                                    f"(limit {limit:.0%}: {'below' if args.week_km <= baseline else 'above'} baseline)."))

    # P6: weeks since a recovery week
    since = None
    for i in range(len(full_weeks) - 1, 2, -1):
        prior = statistics.mean(kms[i - 3:i])
        if prior and kms[i] <= 0.75 * prior:
            since = len(full_weeks) - 1 - i
            break
    if since is None:
        findings.append(("WARN", "P6", "No recovery week (<= 75% of the 3 weeks before) in the last 12 weeks."))
    else:
        findings.append(("WARN" if since >= 3 else "OK", "P6",
                         f"Last recovery week was {since + 1} week(s) ago (every 3rd-4th week)."))

    # P7: volume and intensity up together
    if last["km"] > prev["km"] * 1.05 and last["hard"] > prev["hard"]:
        findings.append(("WARN", "P7", "Last week raised both volume and hard sessions."))

    # P8: single-run spike
    if args.long_run:
        cutoff = today - timedelta(days=30)
        recent = [run_km(a) for a in runs if date.fromisoformat(a["startTimeLocal"][:10]) >= cutoff]
        longest30 = max(recent) if recent else 0
        spike = (args.long_run - longest30) / longest30 if longest30 else 0
        band = ("<= 10% (reference)" if spike <= 0.10 else "10-30% spike: injury hazard ~1.6x" if spike <= 0.30
                else "30-100% spike: hazard ~1.5x" if spike <= 1.0 else "> 100% spike: hazard ~2.3x")
        findings.append(("WARN" if spike > 0.10 else "OK", "P8",
                         f"Long run {args.long_run:.1f} km vs longest in 30 days {longest30:.1f} km: "
                         f"{spike:+.0%} ({band}). 10% cap: {longest30 * 1.1:.1f} km. "
                         "Lifetime capacity: see brain_athlete_history.md."))
        if args.week_km:
            ls = args.long_run / args.week_km
            findings.append(("WARN" if ls > LONG_RUN_SHARE_MAX else "OK", "P9",
                             f"Long run = {ls:.0%} of the proposed week (<= 35%)."))
    else:
        ls = last["long"] / last["km"] if last["km"] else 0
        findings.append(("WARN" if ls > LONG_RUN_SHARE_MAX else "OK", "P9",
                         f"Last week's long run was {ls:.0%} of its km (<= 35%)."))

    # P15: hard sessions per week
    hard = args.hard if args.hard is not None else last["hard"]
    findings.append(("WARN" if hard > 2 else "OK", "P15",
                     f"{'Proposed' if args.hard is not None else 'Last week'}: {hard} hard session(s) (1-2)."))

    print()
    for lvl, pid, msg in findings:
        print(f"[{lvl:4}] {pid:3}  {msg}")


if __name__ == "__main__":
    main()
