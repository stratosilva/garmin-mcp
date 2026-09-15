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
