import datetime
import importlib.util
import sys
import types
import unittest
from pathlib import Path


if "starlette.responses" not in sys.modules:
    responses = types.ModuleType("starlette.responses")
    responses.HTMLResponse = type("HTMLResponse", (), {})
    responses.JSONResponse = type("JSONResponse", (), {})
    routing = types.ModuleType("starlette.routing")
    routing.Route = type("Route", (), {})
    sys.modules.setdefault("starlette", types.ModuleType("starlette"))
    sys.modules["starlette.responses"] = responses
    sys.modules["starlette.routing"] = routing


MODULE_PATH = Path(__file__).parents[1] / "src" / "garmin_mcp" / "dashboard.py"
SPEC = importlib.util.spec_from_file_location("dashboard_fitness_under_test", MODULE_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)

TODAY = datetime.date(2026, 9, 15)


def activity(days_ago, effort, load=None):
    return {"date": (TODAY - datetime.timedelta(days=days_ago)).isoformat(),
            "effort": effort, "load": load if load is not None else effort * 2.1}


def steady(days, effort_per_day, **kwargs):
    return [activity(offset, effort_per_day, **kwargs) for offset in range(days)]


class FitnessInputTests(unittest.TestCase):
    def test_fitness_tracks_effort_points_not_garmin_load(self):
        """Strava builds Fitness from Relative Effort; Garmin Load is ~2x that."""
        series = dashboard._training_history(steady(400, 10.0, load=100.0), TODAY)
        self.assertAlmostEqual(series[-1]["fitness"], 10.0, delta=0.5)

    def test_the_index_is_unaffected_by_garmin_load_magnitude(self):
        low = dashboard._training_history(steady(400, 10.0, load=1.0), TODAY)
        high = dashboard._training_history(steady(400, 10.0, load=999.0), TODAY)
        self.assertEqual(low[-1]["fitness"], high[-1]["fitness"])

    def test_garmin_load_is_still_reported_for_reference(self):
        series = dashboard._training_history(steady(60, 10.0, load=21.0), TODAY)
        self.assertEqual(series[-1]["garminLoad"], 21.0)

    def test_days_without_heart_rate_fall_back_to_scaled_garmin_load(self):
        rows = [{"date": (TODAY - datetime.timedelta(days=o)).isoformat(),
                 "effort": None, "load": 30.0} for o in range(400)]
        series = dashboard._training_history(rows, TODAY)
        self.assertAlmostEqual(series[-1]["fitness"], 10.0, delta=0.5)


class MissingZoneDataTests(unittest.TestCase):
    """Activities recorded before the watch logged time-in-zone.

    Those minutes used to fall to the below-zone-1 floor, scoring a hard hour
    as if it were a stroll. The rate curve estimates the aerobic term from
    average heart rate instead; it is fitted to activities that do carry zone
    minutes and reproduces 95% of their aerobic total on a holdout.
    """

    def session(self, avg_hr, minutes, zones=None):
        return dashboard._map_activity({
            "activityType": {"typeKey": "running"},
            "duration": minutes * 60, "distance": 0,
            "averageHR": avg_hr, "activityTrainingLoad": 0,
            **{f"hrTimeInZone_{i}": (zones[i - 1] * 60 if zones else 0) for i in range(1, 6)},
        })

    def test_the_rate_climbs_with_heart_rate(self):
        rates = [dashboard._aerobic_rate(hr) for hr in (95, 110, 125, 140, 155)]
        self.assertEqual(rates, sorted(rates))

    def test_the_rate_is_flat_outside_the_fitted_range(self):
        self.assertEqual(dashboard._aerobic_rate(40), dashboard._aerobic_rate(90))
        self.assertEqual(dashboard._aerobic_rate(200), dashboard._aerobic_rate(165))

    def test_no_heart_rate_and_no_zones_scores_nothing(self):
        self.assertIsNone(self.session(None, 60)["effort"])

    def test_a_hard_hour_without_zones_is_no_longer_scored_as_a_stroll(self):
        easy = self.session(95, 60)["effort"]
        hard = self.session(140, 60)["effort"]
        self.assertGreater(hard, easy * 2)

    def test_the_estimate_sits_between_the_old_floor_and_an_all_out_hour(self):
        """The fitted rate is an average over real sessions at that heart rate.

        It must clear the below-zone-1 floor the old code applied, while staying
        well under a session spent entirely in that zone - a 130 bpm average
        includes warm-up and recovery minutes, not just tempo ones.
        """
        estimated = self.session(130, 60)["effort"]
        old_floor = 60 * 1.2 / 10.0                    # what the old code scored
        all_in_zone_three = 60 * 8 / 10.0              # an hour entirely in Z3
        self.assertGreater(estimated, old_floor)
        self.assertLess(estimated, all_in_zone_three)

    def test_recorded_zones_still_take_precedence(self):
        """The estimate must never override a real breakdown."""
        row = self.session(130, 60, zones=[0, 0, 0, 0, 60])
        self.assertAlmostEqual(row["effortZonePart"], 60 * 22 / 10.0, delta=0.1)


class FitnessWarmupTests(unittest.TestCase):
    def test_the_series_does_not_start_from_a_cold_zero(self):
        """A cold start understates early points and inflates long-period gains."""
        series = dashboard._training_history(steady(800, 10.0), TODAY)
        self.assertAlmostEqual(series[0]["fitness"], 10.0, delta=0.5)

    def test_steady_training_shows_no_long_term_change(self):
        series = dashboard._training_history(steady(800, 10.0), TODAY)
        first, last = series[0]["fitness"], series[-1]["fitness"]
        self.assertAlmostEqual((last - first) / first * 100, 0.0, delta=5.0)

    def test_a_genuine_decline_still_reads_as_a_decline(self):
        activities = steady(800, 12.0)[120:] + steady(120, 6.0)
        series = dashboard._training_history(activities, TODAY)
        self.assertLess(series[-1]["fitness"], series[0]["fitness"])

    def test_a_genuine_build_still_reads_as_a_build(self):
        activities = steady(800, 5.0)[120:] + steady(120, 15.0)
        series = dashboard._training_history(activities, TODAY)
        self.assertGreater(series[-1]["fitness"], series[0]["fitness"])


class ResponseConstantTests(unittest.TestCase):
    """Guards the smoothing that keeps percentage changes comparable to Strava.

    A constant fitted against Strava's own weekly Relative Effort was tried and
    reverted: these effort points swing harder than Strava's, so a constant
    tuned on the smoother series tracked this one too closely and inflated
    every percentage change. The test below expresses the property that
    actually matters - the curve must be smoother than its input.
    """

    def test_fitness_swings_less_than_the_training_that_drives_it(self):
        quiet, heavy = 3.0, 20.0
        activities = steady(400, quiet)[60:] + steady(60, heavy)
        series = dashboard._training_history(activities, TODAY)
        by = {row["date"]: row["fitness"] for row in series}
        before = by[(TODAY - datetime.timedelta(days=60)).isoformat()]
        now = series[-1]["fitness"]
        # Daily effort jumped 6.7x; a month and a half later the index must
        # still be well short of that, or percentage changes read as nonsense.
        self.assertLess(now / before, heavy / quiet)

    def test_fatigue_stays_on_the_classic_seven_day_response(self):
        self.assertEqual(dashboard.FATIGUE_DAYS, 7.0)

    def test_fitness_responds_more_slowly_than_fatigue(self):
        self.assertGreater(dashboard.FITNESS_DAYS, dashboard.FATIGUE_DAYS * 3)

    def test_scale_changes_points_without_changing_percentages(self):
        from unittest.mock import patch
        activities = steady(400, 4.0)[30:] + steady(30, 15.0)
        scaled = dashboard._training_history(activities, TODAY)
        with patch.object(dashboard, "FITNESS_SCALE", 1.0):
            unscaled = dashboard._training_history(activities, TODAY)
        for index in (-1, -32):
            self.assertAlmostEqual(scaled[index]["fitness"], unscaled[index]["fitness"] * 1.2, places=5)
        self.assertAlmostEqual(scaled[-1]["fitness"] / scaled[-32]["fitness"],
                               unscaled[-1]["fitness"] / unscaled[-32]["fitness"], places=5)

    def test_calibration_preserves_relative_effort_and_fatigue(self):
        from unittest.mock import patch
        import copy
        activities = steady(400, 10.0)
        for row in activities:
            row["hr"] = 160.0
        original = copy.deepcopy(activities)
        calibrated = dashboard._training_history(activities, TODAY)
        with patch.object(dashboard, "_fitness_effort", side_effect=lambda activity, effort: effort):
            baseline = dashboard._training_history(activities, TODAY)
        self.assertEqual(activities, original)
        for before, after in zip(baseline, calibrated):
            for key in ("effort", "load", "garminLoad", "fatigue"):
                self.assertEqual(before[key], after[key])
        self.assertGreater(calibrated[-1]["fitness"], baseline[-1]["fitness"])

    def test_intensity_adjustment_preserves_zone_based_input(self):
        easy = dashboard._fitness_effort({"hr": 139}, 25.7)
        hard = dashboard._fitness_effort({"hr": 161}, 185.6)
        self.assertAlmostEqual(easy, 33.815, delta=0.01)
        self.assertAlmostEqual(hard, 379.365, delta=0.1)
        self.assertAlmostEqual(dashboard._fitness_effort({"hr": 139}, 51.4), easy * 2)



class FitnessResponseTests(unittest.TestCase):
    def test_fatigue_responds_faster_than_fitness(self):
        activities = steady(800, 4.0)[20:] + steady(20, 40.0)
        series = dashboard._training_history(activities, TODAY)
        self.assertGreater(series[-1]["fatigue"], series[-1]["fitness"])
        self.assertGreater(series[-1]["form"] * -1, 0)   # form is negative when fatigued

    def test_form_is_fitness_minus_fatigue(self):
        series = dashboard._training_history(steady(200, 8.0), TODAY)
        row = series[-1]
        self.assertAlmostEqual(row["form"], row["fitness"] - row["fatigue"], delta=0.11)

    def test_fitness_decays_when_training_stops(self):
        series = dashboard._training_history(steady(800, 10.0)[60:], TODAY)
        self.assertLess(series[-1]["fitness"], 4.0)     # 60 idle days, 42-day constant
        self.assertGreater(series[-1]["fitness"], 0.0)  # but has not hit zero

    def test_two_year_window_is_returned(self):
        series = dashboard._training_history(steady(800, 10.0), TODAY)
        self.assertLessEqual(len(series), 732)
        self.assertGreaterEqual(len(series), 729)

    def test_empty_history_is_safe(self):
        series = dashboard._training_history([], TODAY)
        self.assertEqual([row["fitness"] for row in series], [0.0])

    def test_malformed_dates_are_skipped(self):
        series = dashboard._training_history(
            [{"date": "not-a-date", "effort": 5}, {"date": None, "effort": 5}] + steady(60, 10.0),
            TODAY)
        self.assertTrue(series)


if __name__ == "__main__":
    unittest.main()
