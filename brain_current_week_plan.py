# CURRENT WEEKLY PLAN (SAMPLE DATA: the public copy ships an illustrative week, not a real one)
# This file is the single source of truth for the active training week.

week_start = "2026-01-05"
week_end   = "2026-01-11"
generated  = "2026-01-04"

plan = {
    "meta": {
        "week":        "January 5 – January 11, 2026 (sample)",
        "phase":       "Roadmap Phase A (raise the floor) | consolidation week",
        "load_target": "~50 km | Long Run: 20 km Sat, last 5 km at marathon effort",
        "objective":   "Hold volume (P5), add one threshold session and strides (P1, P10), one light leg day.",
        "facility_note":"Weekdays in the work city; weekend in the home town (ask the athlete first).",
    },
    "days": {
        "2026-01-05": {"label": "Monday | Legs (light)", "session": "Single-leg strength, 2 sets each at RPE 6. No run.",
                       "detail": "", "run_km": 0, "gym": True, "pool": False, "plyo": False},
        "2026-01-06": {"label": "Tuesday | Easy 10K + strides + Upper Pull", "session": "10 km easy + 6 x 20 s strides; pull day.",
                       "detail": "", "run_km": 10.0, "gym": True, "pool": False, "plyo": False},
        "2026-01-07": {"label": "Wednesday | Threshold 4 x 8 min", "session": "Treadmill cruise intervals.",
                       "detail": "", "run_km": 11.0, "gym": True, "pool": False, "plyo": False},
        "2026-01-08": {"label": "Thursday | Upper Push", "session": "Push day. No run.",
                       "detail": "", "run_km": 0, "gym": True, "pool": False, "plyo": False},
        "2026-01-09": {"label": "Friday | Shakeout 5K", "session": "5 km very easy + 4 strides.",
                       "detail": "", "run_km": 5.0, "gym": False, "pool": False, "plyo": False},
        "2026-01-10": {"label": "Saturday | Long Run 20K", "session": "14 km easy, 5 km marathon effort, 1 km easy.",
                       "detail": "", "run_km": 20.0, "gym": False, "pool": False, "plyo": False},
        "2026-01-11": {"label": "Sunday | Recovery 5K", "session": "5 km very easy or 40 min easy bike.",
                       "detail": "", "run_km": 5.0, "gym": False, "pool": False, "plyo": False},
    }
}

if __name__ == "__main__":
    total = sum(d["run_km"] for d in plan["days"].values())
    print(f"=== WEEKLY PLAN: {plan['meta']['week']} ===  planned running: {total:.1f} km")
