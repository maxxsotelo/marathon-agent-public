"""
Tests for sensor_pre_schedule_check.py against a simulated Garmin account.

Each test builds a small training history relative to "now", runs the gate's main() with
CLI arguments and checks the exit code: 0 cleared, 1 blocked, 2 consult.

    python -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GARMIN_TOKENS", "")   # never used: get_client is patched

import core_common  # noqa: E402
import sensor_pre_schedule_check as gate  # noqa: E402

NOW = core_common.now_pht()
TODAY = NOW.date()


def run(km, hours_ago, hr=148, te=2.5, kind="running", mins=None):
    start = NOW - timedelta(hours=hours_ago)
    mins = mins or km * 5.5
    return {"activityId": int(start.timestamp()), "activityName": f"run {km}",
            "activityType": {"typeKey": kind}, "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
            "distance": km * 1000, "duration": mins * 60, "averageHR": hr,
            "aerobicTrainingEffect": te, "anaerobicTrainingEffect": 0.0}


def steady_history(weeks=4, per_week=(8, 8, 8, 16)):
    """~40 km/week of easy running, the last long run 6 days ago."""
    acts = []
    for w in range(weeks):
        for i, km in enumerate(per_week):
            acts.append(run(km, hours_ago=24 * (7 * w + 2 * i + 1) + 24 * 6 * (km == 16)))
    return acts


class FakeGarmin:
    def __init__(self, acts=None, last_night=115, weekly=112, balanced_low=105,
                 rhr_today=40, rhr_normal=40, hrv_missing=False, calendar=None, fail_acts=False):
        self.acts = acts if acts is not None else steady_history()
        self.hrv = dict(last_night=last_night, weekly=weekly, balanced_low=balanced_low)
        self.rhr_today, self.rhr_normal = rhr_today, rhr_normal
        self.hrv_missing, self.calendar, self.fail_acts = hrv_missing, calendar or [], fail_acts

    def get_activities_by_date(self, start, end, activitytype=None):
        if self.fail_acts:
            raise ConnectionError("Garmin down")
        return [a for a in self.acts if start <= a["startTimeLocal"][:10] <= end]

    def get_hrv_data_range(self, start, end):
        if self.hrv_missing:
            return {"hrvSummaries": []}
        out = []
        for i in range(28):
            d = (TODAY - timedelta(days=i)).isoformat()
            out.append({"calendarDate": d, "lastNightAvg": self.hrv["last_night"] if i == 0 else 112,
                        "weeklyAvg": self.hrv["weekly"],
                        "baseline": {"balancedLow": self.hrv["balanced_low"]}})
        return {"hrvSummaries": out}

    def get_rhr_daily(self, start, end):
        return [{"calendarDate": (TODAY - timedelta(days=i)).isoformat(),
                 "value": self.rhr_today if i == 0 else self.rhr_normal} for i in range(29)]

    def get_sleep_data(self, d):
        return {"dailySleepDTO": {"sleepScores": {"overall": {"value": 85}}, "sleepTimeSeconds": 28000}}

    def get_hrv_data(self, d):
        return {"hrvSummary": {"lastNightAvg": self.hrv["last_night"], "weeklyAvg": self.hrv["weekly"],
                               "baseline": {"balancedLow": self.hrv["balanced_low"]}}}

    def get_user_summary(self, d):
        return {"averageStressLevel": 20}

    def get_activity_splits(self, aid):
        return {"lapDTOs": []}

    def get_scheduled_workouts(self, y, m):
        return {"calendarItems": self.calendar}

    def get_activity_exercise_sets(self, aid):
        return {"exerciseSets": []}


def gate_exit(client, *argv, plan=None, trs=90.0):
    """Run the gate; return its exit code."""
    with mock.patch.object(gate, "get_client", return_value=client), \
         mock.patch.object(gate, "plan", plan or {"meta": {"week": "test"}, "days": {}}), \
         mock.patch.object(gate, "readiness", return_value={"trs": trs, "fbi": None, "notes": []}), \
         mock.patch.object(sys, "argv", ["gate", *argv]), \
         contextlib.redirect_stdout(io.StringIO()):
        try:
            gate.main()
        except SystemExit as e:
            return e.code
    return 0


class TestHRVOptionB(unittest.TestCase):
    def test_normal_day_clears_intervals(self):
        self.assertEqual(gate_exit(FakeGarmin(), "--type", "intervals", "--duration", "50",
                                   "--intensity", "vo2max"), 0)

    def test_low_night_and_low_week_blocks_z45(self):
        c = FakeGarmin(last_night=95, weekly=100)
        self.assertEqual(gate_exit(c, "--type", "tempo", "--duration", "50", "--intensity", "threshold"), 1)

    def test_single_low_night_is_only_a_warning(self):
        c = FakeGarmin(last_night=100, weekly=110)
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 0)

    def test_low_night_plus_raised_rhr_blocks(self):
        c = FakeGarmin(last_night=100, weekly=110, rhr_today=46, rhr_normal=40)
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 1)

    def test_trip_still_allows_easy_and_long_runs(self):
        c = FakeGarmin(last_night=95, weekly=100)
        self.assertEqual(gate_exit(c, "--type", "easy", "--duration", "45"), 0)
        self.assertEqual(gate_exit(c, "--type", "long_run", "--duration", "100"), 0)

    def test_marathon_pace_capped_at_20_min_on_trip(self):
        c = FakeGarmin(last_night=95, weekly=100)
        base = ["--type", "long_run", "--duration", "100", "--intensity", "marathon"]
        self.assertEqual(gate_exit(c, *base, "--mp-minutes", "30"), 1)
        self.assertEqual(gate_exit(c, *base, "--mp-minutes", "15"), 0)

    def test_missing_hrv_blocks_hard_clears_easy(self):
        c = FakeGarmin(hrv_missing=True)
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 1)
        self.assertEqual(gate_exit(c, "--type", "easy", "--duration", "45"), 0)

    def test_lifting_on_trip_day_is_a_consult(self):
        c = FakeGarmin(last_night=95, weekly=100)
        self.assertEqual(gate_exit(c, "--type", "upper_body", "--duration", "50"), 2)

    def test_hiit_cardio_treated_as_z45(self):
        c = FakeGarmin(last_night=95, weekly=100)
        self.assertEqual(gate_exit(c, "--type", "hiit", "--duration", "25"), 1)
        self.assertEqual(gate_exit(c, "--type", "easy_cardio", "--duration", "40"), 0)


class TestACWR(unittest.TestCase):
    def test_spike_blocks_any_run(self):
        acts = steady_history() + [run(15, hours_ago=24 * d + 3) for d in range(1, 6)]
        self.assertEqual(gate_exit(FakeGarmin(acts=acts), "--type", "easy", "--duration", "60"), 1)

    def test_future_date_uses_the_window_ending_that_day(self):
        # A heavy last 5 days blocks today, but a week later those runs are outside the
        # 7-day window; planned runs in between are counted.
        acts = steady_history() + [run(15, hours_ago=24 * d + 3) for d in range(1, 6)]
        later = (TODAY + timedelta(days=7)).isoformat()
        self.assertEqual(gate_exit(FakeGarmin(acts=acts), "--type", "easy", "--duration", "60",
                                   "--date", later), 0)
        heavy_plan = {"meta": {"week": "test"},
                      "days": {(TODAY + timedelta(days=i)).isoformat(): {"label": "", "session": "",
                                                                         "run_km": 20} for i in range(2, 7)}}
        self.assertEqual(gate_exit(FakeGarmin(acts=acts), "--type", "easy", "--duration", "60",
                                   "--date", later, plan=heavy_plan), 1)

    def test_garmin_down_fails_closed_for_hard_only(self):
        c = FakeGarmin(fail_acts=True)
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 1)
        self.assertEqual(gate_exit(c, "--type", "easy", "--duration", "45"), 0)


class TestRecoveryHours(unittest.TestCase):
    def history_with_long_run(self, hours_ago):
        return steady_history() + [run(20, hours_ago=hours_ago, te=3.5)]

    def test_hard_within_24h_of_long_run_blocked(self):
        c = FakeGarmin(acts=self.history_with_long_run(12))
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 1)

    def test_hard_24_to_48h_is_consult(self):
        c = FakeGarmin(acts=self.history_with_long_run(30))
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 2)

    def test_hard_after_48h_clears(self):
        c = FakeGarmin(acts=self.history_with_long_run(60))
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max"), 0)

    def test_marathon_pace_follows_the_same_rule(self):
        c = FakeGarmin(acts=self.history_with_long_run(12))
        self.assertEqual(gate_exit(c, "--type", "easy", "--duration", "60", "--intensity", "marathon"), 1)

    def test_heavy_legs_within_48h_is_consult(self):
        c = FakeGarmin(acts=self.history_with_long_run(30))
        self.assertEqual(gate_exit(c, "--type", "legs_heavy", "--duration", "50"), 2)
        self.assertEqual(gate_exit(c, "--type", "legs_light", "--duration", "40"), 0)


class TestLongRunAhead(unittest.TestCase):
    def test_heavy_legs_72h_before_planned_long_run_blocked(self):
        plan = {"meta": {"week": "test"},
                "days": {(TODAY + timedelta(days=2)).isoformat(): {"label": "Sat", "session": "Long run 22K",
                                                                    "run_km": 22}}}
        self.assertEqual(gate_exit(FakeGarmin(), "--type", "legs_heavy", "--duration", "50", plan=plan), 1)
        self.assertEqual(gate_exit(FakeGarmin(), "--type", "legs_light", "--duration", "40", plan=plan), 0)

    def test_long_run_on_garmin_calendar_also_counts(self):
        cal = [{"itemType": "workout", "title": "W17D6: Long Run 20K",
                "date": (TODAY + timedelta(days=1)).isoformat()}]
        self.assertEqual(gate_exit(FakeGarmin(calendar=cal), "--type", "legs_heavy", "--duration", "50"), 1)


class TestReadinessAndFuture(unittest.TestCase):
    def test_trs_below_50_caps_at_zone2(self):
        c = FakeGarmin()
        self.assertEqual(gate_exit(c, "--type", "tempo", "--duration", "50", "--intensity", "threshold",
                                   trs=40.0), 1)
        self.assertEqual(gate_exit(c, "--type", "easy", "--duration", "45", trs=40.0), 0)

    def test_future_date_is_provisional(self):
        c = FakeGarmin(last_night=95, weekly=100)    # today's low HRV must not judge a future day
        d = (TODAY + timedelta(days=3)).isoformat()
        self.assertEqual(gate_exit(c, "--type", "intervals", "--duration", "50", "--intensity", "vo2max",
                                   "--date", d), 0)


if __name__ == "__main__":
    unittest.main()
