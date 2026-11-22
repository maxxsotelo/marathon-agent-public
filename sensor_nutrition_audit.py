"""
Nutrition audit: calories eaten vs. burned and energy availability (EA) per day.

Calories come from MyFitnessPal via the Garmin Connect sync. Garmin only receives
the calorie total, not macros, so protein/carbs/fat are logged separately in
brain_nutrition_log.md (from MyFitnessPal screenshots the athlete sends).

EA = (calories eaten - exercise calories) / lean mass. Exercise calories are the
calories of every logged activity except walking. Rule 7 targets: EA >= 45, flag < 30.

Usage:
    python sensor_nutrition_audit.py [DAYS] [END_DATE]
    DAYS defaults to 7, END_DATE (YYYY-MM-DD) defaults to yesterday (PHT), since
    today's log is usually incomplete.
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from core_common import LEAN_MASS_KG, PHT
from garmin_auth import get_client

EA_TARGET = 45
EA_FLAG = 30


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    end = (date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2
           else datetime.now(PHT).date() - timedelta(days=1))
    start = end - timedelta(days=days - 1)

    client = get_client()

    exercise_kcal = defaultdict(float)
    sessions = defaultdict(list)
    for act in client.get_activities_by_date(start.isoformat(), end.isoformat()):
        type_key = act.get("activityType", {}).get("typeKey", "")
        if type_key == "walking":
            continue
        day = act.get("startTimeLocal", "")[:10]
        exercise_kcal[day] += act.get("calories") or 0
        sessions[day].append(type_key.replace("_", " "))

    print(f"=== NUTRITION AUDIT: {start} to {end} ===")
    print("Date        Eaten  Burned    Net  Exercise   EA  Sessions")
    eaten_days = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        summary = client.get_user_summary(day)
        eaten = summary.get("consumedKilocalories") or 0
        burned = summary.get("totalKilocalories") or 0
        ex = exercise_kcal[day]
        names = ", ".join(sessions[day]) or "rest"
        if not eaten:
            print(f"{day}  {'--':>5}  {burned:6.0f}  {'--':>5}  {ex:8.0f}  {'--':>4}  {names}  [no food logged]")
            continue
        ea = (eaten - ex) / LEAN_MASS_KG
        flag = "  << LOW EA" if ea < EA_FLAG else ""
        print(f"{day}  {eaten:5.0f}  {burned:6.0f}  {eaten - burned:+5.0f}  {ex:8.0f}  {ea:4.1f}  {names}{flag}")
        eaten_days.append((eaten, burned, ea))

    if eaten_days:
        n = len(eaten_days)
        avg_eaten = sum(e for e, _, _ in eaten_days) / n
        avg_burned = sum(b for _, b, _ in eaten_days) / n
        avg_ea = sum(a for _, _, a in eaten_days) / n
        low = sum(1 for _, _, a in eaten_days if a < EA_FLAG)
        print(f"\nLogged days: {n}/{days} | Avg eaten {avg_eaten:.0f} | Avg burned {avg_burned:.0f} "
              f"| Net {avg_eaten - avg_burned:+.0f}/day")
        print(f"Avg EA {avg_ea:.1f} kcal/kg lean (target >= {EA_TARGET}) | Days below {EA_FLAG}: {low}")
    else:
        print("\nNo food logged in this window.")


if __name__ == "__main__":
    main()
