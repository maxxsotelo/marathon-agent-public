"""The learning layer beyond the acceptance tests: regression helper, run selection, the three models and the
weekly retrain (scripts/model_utils.py, efficiency_model.py, pace_model.py, habit_model.py, learn_weekly.py)."""

import csv
import io
import json
import os
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("GARMIN_TOKENS", "")

import efficiency_model as em  # noqa: E402
import habit_model as hm  # noqa: E402
import learn_weekly as lw  # noqa: E402
import pace_model as pm  # noqa: E402
from ledger import ACTIVITY_COLUMNS, PLAN_COLUMNS  # noqa: E402
from model_utils import clear, predict, steady_runs, wls  # noqa: E402

TODAY = date(2026, 11, 22)


def ledger_runs(n_days=150, seed=3):
    """Ledger rows for an athlete whose speed at a given HR rises ~1 %/month, slower in heat and on hills."""
    rnd = random.Random(seed)
    rows = []
    for i, d in enumerate(range(1, n_days, 2)):
        day = TODAY - timedelta(days=d)
        hr = rnd.uniform(150, 174)
        heat, climb = rnd.uniform(25, 35), rnd.uniform(0, 12)
        speed = (1.2 + 0.010 * hr) * (1 - 0.006 * (heat - 29)) * (1 - 0.004 * climb) * (1 + 0.0003 * (n_days - d))
        km = rnd.uniform(5.5, 14)
        rows.append({"activity_id": str(1000 + i), "date": day.isoformat(), "start": "06:00", "type": "running",
                     "name": "Easy run", "treadmill": 0, "km": round(km, 2), "moving_min": round(km * 1000 / speed / 60, 1),
                     "avg_hr": round(hr), "avg_speed_mps": round(speed, 3), "anaerobic_te": 0.3,
                     "heat_index_c": round(heat, 1), "climb_per_km": round(climb, 1)})
    return rows


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


class Regression(unittest.TestCase):
    def test_recovers_coefficients_drops_flat_feature_and_reports_uncertainty(self):
        rnd = random.Random(1)
        rows = [{"y": 2 + 0.5 * x - 0.2 * z + rnd.gauss(0, 0.01), "x": x, "z": z, "flat": 4.0}
                for x, z in ((rnd.uniform(0, 10), rnd.uniform(0, 5)) for _ in range(80))]
        m = wls(rows, "y", ["x", "z", "flat"])
        self.assertAlmostEqual(m["coef"]["x"], 0.5, places=2)
        self.assertAlmostEqual(m["coef"]["z"], -0.2, places=2)
        self.assertEqual(m["dropped"], ["flat"])
        self.assertTrue(clear(m, "x"))
        self.assertAlmostEqual(predict(m, x=2, z=1, flat=99), 2.8, places=1)    # a dropped feature adds nothing

    def test_noise_is_not_clear(self):
        rnd = random.Random(2)
        rows = [{"y": rnd.gauss(0, 1), "x": rnd.uniform(0, 1)} for _ in range(25)]
        self.assertFalse(clear(wls(rows, "y", ["x"]), "x"))

    def test_too_few_or_missing(self):
        self.assertIsNone(wls([{"y": 1, "x": 1}, {"y": 2, "x": None}], "y", ["x"]))


class SteadyRuns(unittest.TestCase):
    def test_selection(self):
        base = {"type": "running", "treadmill": 0, "km": 8, "avg_hr": 165, "avg_speed_mps": 3.0, "anaerobic_te": 0.5,
                "name": "Easy", "date": "2026-11-20", "heat_index_c": 30, "climb_per_km": 4}
        picked = steady_runs([base, dict(base, treadmill=1), dict(base, km=4), dict(base, avg_hr=182),
                              dict(base, anaerobic_te=2.4), dict(base, name="5K time trial"),
                              dict(base, type="strength_training")])
        self.assertEqual(len(picked), 1)
        self.assertAlmostEqual(picked[0]["ef"], 3.0 * 60 / 165)


class Efficiency(unittest.TestCase):
    def test_trend_and_report_lines(self):
        runs = steady_runs(ledger_runs())
        m = em.fit(runs, today=TODAY, half_life_days=120)
        self.assertIn("weeks", m["coef"])
        self.assertGreater(m["coef"]["weeks"], 0)                       # fitness improving, heat-corrected
        self.assertLess(m["coef"]["heat_index_c"], 0)
        text = " ".join(em.describe(m))
        self.assertIn("heat index", text)
        self.assertIn("clear", text)
        with tempfile.TemporaryDirectory() as d:
            em.save(m, os.path.join(d, "eff.json"), generated=TODAY)
            back = em.load(os.path.join(d, "eff.json"), today=TODAY)
            self.assertAlmostEqual(em.expected_ef(back, 30, 4), em.expected_ef(m, 30, 4))
            self.assertIsNone(em.load(os.path.join(d, "eff.json"), today=TODAY + timedelta(days=30)))


class Pace(unittest.TestCase):
    def test_bands_only_where_runs_reach(self):
        runs = steady_runs(ledger_runs())
        m = pm.fit_hr_speed(runs, TODAY)
        cover = pm.hr_coverage(runs, TODAY)
        self.assertTrue(pm.band_supported(162, 174, cover))
        self.assertFalse(pm.band_supported(182, 191, cover))           # no threshold band from easy runs
        fast, slow = pm.pace_band(m, 162, 174)
        self.assertLess(fast, slow)

    def test_no_recent_runs(self):
        old = [dict(r, date=(TODAY - timedelta(days=60 + i)).isoformat()) for i, r in enumerate(steady_runs(ledger_runs()))]
        self.assertIsNone(pm.fit_hr_speed(old, TODAY))


class Habits(unittest.TestCase):
    def rows(self, weeks=10, skip_every=3):
        out = []
        start = date(2026, 9, 7)                                         # a Monday
        for w in range(weeks):
            for wd, kind, km, actual in (("Tue", "easy", 6.0, 7.2), ("Thu", "easy", 6.0, 6.0), ("Sat", "long_run", 16.0, 16.5),
                                         ("Sun", "rest", 0.0, 3.0 if w % 2 else 0.0)):
                day = start + timedelta(days=w * 7 + ("Mon Tue Wed Thu Fri Sat Sun".split().index(wd)))
                done = "" if kind == "rest" else (0 if (wd == "Thu" and w % skip_every == 0) else 1)
                out.append({"date": day.isoformat(), "weekday": wd, "planned_type": kind, "planned_km": km,
                            "done": done, "actual_km": 0.0 if done == 0 else actual})
        return out

    def test_advice_names_the_patterns_without_feeding_them(self):
        advice = " ".join(hm.planning_advice(self.rows(skip_every=1))).lower()
        self.assertIn("thursday", advice)
        self.assertIn("ceiling", advice)                                # overrun: never plan more to match
        self.assertIn("rest", advice)                                   # runs on rest days counted

    def test_skip_model(self):
        p_thu = hm.skip_probability(self.rows(), "Thu", "easy", 6.0)
        p_sat = hm.skip_probability(self.rows(), "Sat", "long_run", 16.0)
        self.assertGreater(p_thu, p_sat)
        self.assertIsNone(hm.skip_probability(self.rows(weeks=4), "Thu", "easy", 6.0))     # too short a history
        never = [dict(r, done=1 if r["done"] != "" else "") for r in self.rows()]
        self.assertIsNone(hm.skip_probability(never, "Thu", "easy", 6.0))                 # no skips to learn from


class Weekly(unittest.TestCase):
    def test_writes_report_bands_and_model_and_caps_the_move(self):
        with tempfile.TemporaryDirectory() as d:
            ledger, out = Path(d) / "ledger", Path(d) / "out"
            ledger.mkdir()
            write_csv(ledger / "activities.csv", ACTIVITY_COLUMNS, ledger_runs())
            write_csv(ledger / "plan.csv", PLAN_COLUMNS, Habits().rows())
            bands_file = out / "pace_bands.json"
            out.mkdir()
            bands_file.write_text(json.dumps({"generated": (TODAY - timedelta(days=7)).isoformat(),
                                              "bands": {"easy": {"fast_s_per_km": 200, "slow_s_per_km": 220}}}))
            with mock.patch.object(lw, "REPORTS", str(out / "reports")), \
                    mock.patch.object(pm, "PACE_BANDS_FILE", str(bands_file)), \
                    mock.patch.object(em, "MODEL_FILE", str(out / "efficiency.json")), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                lw.main(["--today", TODAY.isoformat(), "--ledger", str(ledger)])
            report = (out / "reports" / f"{TODAY}.md").read_text()
            self.assertIn("Heat, hills and fitness", report)
            bands = json.loads(bands_file.read_text())
            self.assertEqual(bands["generated"], TODAY.isoformat())
            self.assertEqual(bands["bands"]["easy"]["fast_s_per_km"], 205)        # moved at most 5 s/km
            self.assertTrue(bands["bands"]["easy"]["capped"])
            self.assertNotIn("threshold", bands["bands"])
            self.assertTrue((out / "efficiency.json").exists())

    def test_empty_ledger_waits_and_writes_no_bands(self):
        with tempfile.TemporaryDirectory() as d:
            report, model, bands, summary = lw.build(d, TODAY, {})
            self.assertIsNone(model)
            self.assertIsNone(bands)
            self.assertIn("Waiting for data", report)
            self.assertTrue(any("HR only" in s for s in summary))


class RunCard(unittest.TestCase):
    RUN = {"activityId": 1, "activityName": "Easy", "activityType": {"typeKey": "running"},
           "startTimeLocal": "2026-11-21 06:00:00", "distance": 8000.0, "duration": 2900.0, "movingDuration": 2880.0,
           "averageHR": 165, "averageSpeed": 2.78, "anaerobicTrainingEffect": 0.4, "elevationGain": 32.0,
           "minTemperature": 30.0, "maxTemperature": 34.0}

    def test_line_only_with_a_fresh_model_and_a_steady_run(self):
        import core_run_card as card
        m = em.fit(steady_runs(ledger_runs()), today=TODAY, half_life_days=120)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "eff.json")
            with mock.patch.object(em, "MODEL_FILE", path):
                self.assertIsNone(card.model_line(self.RUN))                      # no model yet
                em.save(m, generated=card.today_pht())
                line = card.model_line(self.RUN)
                self.assertIn("vs expected for these conditions", line)
                self.assertIsNone(card.model_line(dict(self.RUN, activityType={"typeKey": "treadmill_running"})))
                self.assertIsNone(card.model_line(dict(self.RUN, anaerobicTrainingEffect=2.6)))   # intervals


if __name__ == "__main__":
    unittest.main()
