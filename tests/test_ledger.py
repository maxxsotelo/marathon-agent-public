"""scripts/ledger.py beyond the T15 acceptance test: plan snapshots, deviations, morning and backfill (fake Garmin)."""

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("GARMIN_TOKENS", "")

import ledger  # noqa: E402

RUN = {"activityId": 11, "activityName": "Easy", "activityType": {"typeKey": "running"},
       "startTimeLocal": "2026-11-21 17:42:00", "distance": 7000.0, "duration": 2400.0, "movingDuration": 2300.0,
       "averageHR": 150, "maxHR": 165, "aerobicTrainingEffect": 3.0, "anaerobicTrainingEffect": 0.2,
       "elevationGain": 63.0, "minTemperature": 29.0, "maxTemperature": 33.0, "calories": 500,
       "averageRunningCadenceInStepsPerMinute": 174}
LIFT = {"activityId": 12, "activityName": "Push", "activityType": {"typeKey": "strength_training"},
        "startTimeLocal": "2026-11-21 08:00:00", "duration": 3600.0, "averageHR": 95, "calories": 300}
WALK = {"activityId": 13, "activityName": "Walk", "activityType": {"typeKey": "walking"},
        "startTimeLocal": "2026-11-21 19:00:00", "distance": 800.0, "duration": 600.0, "calories": 40}


class FakeGarmin:
    def __init__(self):
        self.summary_calls = 0

    def get_activities_by_date(self, start, end):
        return [a for a in (RUN, LIFT, WALK) if start <= a["startTimeLocal"][:10] <= end]

    def get_activity(self, aid):
        self.summary_calls += 1
        return {"summaryDTO": {"directWorkoutRpe": 60, "directWorkoutFeel": 75}} if aid == 11 else {"summaryDTO": {}}

    def get_hrv_data(self, day):
        return {"hrvSummary": {"lastNightAvg": 120, "weeklyAvg": 125, "baseline": {"balancedLow": 110}}}

    def get_sleep_data(self, day):
        return {"dailySleepDTO": {"sleepTimeSeconds": 6.5 * 3600}}

    def get_user_summary(self, day):
        return {"restingHeartRate": 40, "bodyBatteryMostRecentValue": 60, "totalSteps": 9000,
                "consumedKilocalories": 3000}

    def get_rhr_day(self, day):
        return {}


class TestRows(unittest.TestCase):
    def test_activity_row_fields(self):
        row = ledger.activity_row(RUN, {"directWorkoutRpe": 60, "directWorkoutFeel": 25})
        self.assertEqual((row["km"], row["climb_per_km"], row["rpe"], row["feel"]), (7.0, 9.0, 6.0, "weak"))
        self.assertGreater(row["heat_index_c"], 25)                  # wrist 31 C - 4 C bias, 70 % RH
        self.assertEqual(row["treadmill"], 0)
        tm = ledger.activity_row(dict(RUN, activityType={"typeKey": "treadmill_running"}))
        self.assertEqual((tm["treadmill"], tm["heat_index_c"]), (1, ""))   # no outdoor heat on a treadmill

    def test_plan_type_and_deviation(self):
        self.assertEqual(ledger.plan_type({"gates": [{"type": "upper_body"}, {"type": "intervals"}]}), "intervals")
        self.assertEqual(ledger.plan_type({"gates": [{"type": "legs_heavy"}], "run_km": 0}), "gym")
        self.assertEqual(ledger.plan_type({"run_km": 0}), "rest")
        self.assertEqual([ledger.deviation(p, a) for p, a in ((6, 6.5), (6, 7.5), (6, 4), (6, 0), (0, 3), (0, 0), ("", 5))],
                         ["none", "extended", "shortened", "skipped", "extra", "none", "unknown"])

    def test_completed_plan_day_has_no_planned_km_unless_snapshot(self):
        e = {"run_km": 17.24, "completed": True, "label": "Sat long", "gates": [{"type": "long_run"}]}
        self.assertEqual(ledger.plan_row("2026-11-21", e, snapshot=False)["planned_km"], "")
        self.assertEqual(ledger.plan_row("2026-11-21", e, snapshot=True)["planned_km"], 17.24)


class TestMorningAndBackfill(unittest.TestCase):
    def test_morning_then_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            plans = {"2026-11-21": {"run_km": 5.5, "gates": [{"type": "easy"}]},
                     "2026-11-22": {"run_km": 0, "gates": [{"type": "upper_body"}], "gym": True}}
            orig = ledger.load_plans
            ledger.load_plans = lambda root=None: plans
            try:
                c = FakeGarmin()
                ledger.morning(c, date(2026, 11, 22), d)
                acts = ledger.read_rows(d / "activities.csv")
                self.assertEqual(sorted(a["activity_id"] for a in acts), ["11", "12", "13"])
                run = next(a for a in acts if a["activity_id"] == "11")
                self.assertEqual((run["rpe"], run["feel"]), ("6.0", "strong"))
                daily = {r["date"]: r for r in ledger.read_rows(d / "daily.csv")}
                self.assertEqual(daily["2026-11-21"]["exercise_kcal"], "800")      # walking excluded
                self.assertEqual(daily["2026-11-21"]["ea"], str(round((3000 - 800) / ledger.LEAN_MASS_KG, 1)))
                plan = {r["date"]: r for r in ledger.read_rows(d / "plan.csv")}
                self.assertEqual(plan["2026-11-22"]["plan_source"], "snapshot")
                self.assertEqual((plan["2026-11-21"]["actual_km"], plan["2026-11-21"]["deviation"]), ("7.0", "extended"))
                # a second morning run changes nothing, and the snapshot is kept
                before = (d / "activities.csv").read_text(), (d / "plan.csv").read_text()
                ledger.morning(c, date(2026, 11, 22), d)
                self.assertEqual(before, ((d / "activities.csv").read_text(), (d / "plan.csv").read_text()))
            finally:
                ledger.load_plans = orig

    def test_backfill_skips_ratings_already_known(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            c = FakeGarmin()
            orig_range, orig_plans = ledger.range_daily_rows, ledger.rebuild_plans
            ledger.range_daily_rows = lambda client, s, e: [{"date": "2026-11-21", "hrv": 120}]
            ledger.rebuild_plans = lambda ledger_dir=None, root=None: (0, 0)
            try:
                ledger.backfill(c, date(2026, 11, 20), date(2026, 11, 22), d, pause=0)
                self.assertEqual(c.summary_calls, 2)               # the run and the lift, not the walk
                ledger.backfill(c, date(2026, 11, 20), date(2026, 11, 22), d, pause=0)
                self.assertEqual(c.summary_calls, 3)               # the run is rated now; the lift has no rating yet
            finally:
                ledger.range_daily_rows, ledger.rebuild_plans = orig_range, orig_plans


if __name__ == "__main__":
    unittest.main()
