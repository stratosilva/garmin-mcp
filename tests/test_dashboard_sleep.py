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
SPEC = importlib.util.spec_from_file_location("dashboard_sleep_under_test", MODULE_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)

TODAY = datetime.date(2026, 9, 8)


def nights(hours_by_offset, bed=23.0):
    """Build a sleep series; keys are days before TODAY, values are hours."""
    built = []
    for offset, hours in sorted(hours_by_offset.items(), reverse=True):
        day = TODAY - datetime.timedelta(days=offset)
        built.append({"date": day.isoformat(), "label": day.strftime("%a"),
                      "hours": hours, "seconds": hours * 3600,
                      "bedTime": bed, "wakeTime": (bed + hours) % 24})
    return built


class SleepNeedTests(unittest.TestCase):
    def test_need_blends_history_with_the_age_guideline(self):
        series = nights({offset: 7.0 for offset in range(14)})
        need, guideline, achieved = dashboard._sleep_need_hours(series, age=45)
        self.assertEqual(guideline, 8.0)          # 18-64 band midpoint
        self.assertEqual(achieved, 7.0)
        self.assertEqual(need, 7.5)               # halfway between the two

    def test_need_falls_back_to_the_guideline_without_history(self):
        need, guideline, achieved = dashboard._sleep_need_hours([], age=45)
        self.assertIsNone(achieved)
        self.assertEqual(need, guideline)

    def test_need_stays_inside_sane_bounds_for_a_heavy_sleeper(self):
        series = nights({offset: 12.0 for offset in range(14)})
        need, _, _ = dashboard._sleep_need_hours(series, age=45)
        self.assertLessEqual(need, dashboard.SLEEP_NEED_MAX_HOURS)

    def test_older_athletes_get_the_lower_guideline(self):
        self.assertEqual(dashboard._recommended_sleep_hours(70), 7.5)
        self.assertEqual(dashboard._recommended_sleep_hours(45), 8.0)


class SleepDebtTests(unittest.TestCase):
    def test_meeting_the_need_every_night_leaves_no_debt(self):
        debt = dashboard._sleep_debt(nights({o: 7.5 for o in range(14)}), 7.5, TODAY)
        self.assertEqual(debt["minutes"], 0)
        self.assertEqual(debt["level"], "none")

    def test_short_nights_accumulate_debt(self):
        debt = dashboard._sleep_debt(nights({o: 6.5 for o in range(14)}), 7.5, TODAY)
        self.assertGreater(debt["minutes"], 60)
        self.assertIn(debt["level"], {"low", "moderate", "high"})

    def test_a_surplus_night_cancels_an_earlier_deficit(self):
        deficit_only = dashboard._sleep_debt(nights({0: 7.5, 1: 5.5}), 7.5, TODAY)
        with_surplus = dashboard._sleep_debt(nights({0: 9.5, 1: 5.5}), 7.5, TODAY)
        self.assertGreater(deficit_only["minutes"], 0)
        self.assertLess(with_surplus["minutes"], deficit_only["minutes"])

    def test_a_night_without_the_watch_is_skipped_not_counted_as_zero(self):
        """A missing night must not inject a full night of phantom debt."""
        complete = dashboard._sleep_debt(nights({o: 7.5 for o in range(14)}), 7.5, TODAY)
        with_gap = dashboard._sleep_debt(
            nights({o: 7.5 for o in range(14) if o != 3}), 7.5, TODAY)
        self.assertEqual(with_gap["minutes"], complete["minutes"])

    def test_recent_nights_weigh_more_than_old_ones(self):
        recent_bad = dashboard._sleep_debt(
            nights({**{o: 7.5 for o in range(14)}, 0: 4.0}), 7.5, TODAY)
        old_bad = dashboard._sleep_debt(
            nights({**{o: 7.5 for o in range(14)}, 13: 4.0}), 7.5, TODAY)
        self.assertGreater(recent_bad["minutes"], old_bad["minutes"])

    def test_series_covers_the_whole_window_for_charting(self):
        debt = dashboard._sleep_debt(nights({o: 6.0 for o in range(14)}), 7.5, TODAY)
        self.assertEqual(len(debt["series"]), dashboard.SLEEP_DEBT_DAYS)
        self.assertEqual(debt["series"][-1]["minutes"], debt["minutes"])


class SleepRegularityTests(unittest.TestCase):
    def test_identical_bedtimes_score_full_marks(self):
        result = dashboard._sleep_regularity(nights({o: 7.5 for o in range(10)}, bed=23.0))
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["deviationMinutes"], 0)

    def test_drifting_bedtimes_lose_points(self):
        series = nights({o: 7.5 for o in range(10)})
        for index, night in enumerate(series):
            night["bedTime"] = 22.0 if index % 2 else 1.0   # 22:00 vs 01:00
        result = dashboard._sleep_regularity(series)
        self.assertLess(result["score"], 100)
        self.assertGreater(result["deviationMinutes"], 30)

    def test_midnight_wrap_is_not_treated_as_a_23_hour_swing(self):
        series = nights({o: 7.5 for o in range(10)})
        for index, night in enumerate(series):
            night["bedTime"] = 23.5 if index % 2 else 0.5   # half an hour apart
        result = dashboard._sleep_regularity(series)
        self.assertLessEqual(result["deviationMinutes"], 30)

    def test_too_few_nights_yields_nothing(self):
        self.assertIsNone(dashboard._sleep_regularity(nights({0: 7.5})))


class SleepPayloadParsingTests(unittest.TestCase):
    def test_parses_the_nested_nightly_payload(self):
        night = dashboard._sleep_night({"dailySleepDTO": {
            "calendarDate": "2026-09-08",
            "sleepTimeSeconds": 24780,           # 6h53m, as in the reference night
            "deepSleepSeconds": 4440, "lightSleepSeconds": 15300,
            "remSleepSeconds": 5040, "awakeSleepSeconds": 3000,
            "sleepStartTimestampLocal": 1757372400000,
            "sleepEndTimestampLocal": 1757400180000,
            "sleepScores": {"overall": {"value": 82}},
            "restlessMomentsCount": 12,
        }})
        self.assertEqual(night["date"], "2026-09-08")
        self.assertEqual(night["hours"], 6.9)
        self.assertEqual(night["score"], 82)
        self.assertEqual(night["stages"]["deep"], 74)
        self.assertEqual(night["stages"]["rem"], 84)
        self.assertEqual(night["inBedHours"], 7.7)
        self.assertEqual(night["efficiency"], 89)     # matches the reference 89%
        self.assertEqual(night["restlessMoments"], 12)

    def test_parses_the_flat_range_row_shape(self):
        night = dashboard._sleep_night({
            "calendarDate": "2026-09-07",
            "values": {"totalSleepSeconds": 27000, "deepSleepSeconds": 3600},
        })
        self.assertEqual(night["date"], "2026-09-07")
        self.assertEqual(night["hours"], 7.5)
        self.assertEqual(night["stages"]["deep"], 60)

    def test_rows_without_sleep_are_dropped(self):
        self.assertIsNone(dashboard._sleep_night({"calendarDate": "2026-09-06"}))
        self.assertIsNone(dashboard._sleep_night(None))

    def test_efficiency_never_exceeds_one_hundred(self):
        night = dashboard._sleep_night({"dailySleepDTO": {
            "calendarDate": "2026-09-08", "sleepTimeSeconds": 28800,
            "sleepStartTimestampLocal": 1757372400000,
            "sleepEndTimestampLocal": 1757400180000,
        }})
        self.assertLessEqual(night["efficiency"], 100)


class HrvBaselineTests(unittest.TestCase):
    def test_reads_the_nested_baseline_garmin_actually_returns(self):
        values = dashboard._hrv_values({"hrvSummary": {
            "lastNightAvg": 48, "status": "BALANCED", "weeklyAvg": 46,
            "baseline": {"balancedLow": 42, "balancedUpper": 58, "markerValue": 50},
        }})
        self.assertEqual(values["value"], 48)
        self.assertEqual(values["baselineLow"], 42)
        self.assertEqual(values["baselineHigh"], 58)

    def test_tolerates_the_older_flat_spelling(self):
        values = dashboard._hrv_values({"hrvSummary": {
            "lastNightAvg": 40, "baselineLow": 35, "baselineHigh": 55}})
        self.assertEqual(values["baselineLow"], 35)
        self.assertEqual(values["baselineHigh"], 55)

    def test_empty_payload_is_safe(self):
        self.assertEqual(dashboard._hrv_values(None)["value"], None)

    def test_weekly_average_is_carried_through(self):
        """Garmin sets HRV status from the 7-day average, so we must keep it."""
        values = dashboard._hrv_values({"hrvSummary": {
            "lastNightAvg": 31, "weeklyAvg": 35, "status": "BALANCED"}})
        self.assertEqual(values["weeklyAvg"], 35)

    def test_garmin_status_is_passed_through_untouched(self):
        for status in ("BALANCED", "UNBALANCED", "LOW", "POOR"):
            self.assertEqual(
                dashboard._hrv_values({"hrvSummary": {"lastNightAvg": 40, "status": status}})["status"],
                status)


class HrvContributorTests(unittest.TestCase):
    def contributor(self, hrv_today, series):
        payload = dashboard._recovery_metrics(
            nights({o: 7.0 for o in range(14)}), series, [],
            {"hrv": hrv_today}, {}, 45, TODAY)
        return next((c for c in payload["contributors"] if c["key"] == "hrvBalance"), None)

    def test_a_balanced_status_never_grades_as_attention(self):
        """A low single night must not turn a Balanced status into a warning."""
        item = self.contributor(
            {"status": "BALANCED", "value": 28, "weeklyAvg": 35},
            [{"date": "2026-09-08", "value": 28, "status": "BALANCED"}])
        self.assertEqual(item["grade"], "optimal")
        self.assertEqual(item["detail"], "Balanced")

    def test_the_note_reports_the_seven_day_average_not_last_night(self):
        item = self.contributor(
            {"status": "BALANCED", "value": 28, "weeklyAvg": 35},
            [{"date": "2026-09-08", "value": 28}])
        self.assertIn("35", item["note"])
        self.assertNotIn("28", item["note"])

    def test_weekly_average_is_reconstructed_when_garmin_omits_it(self):
        series = [{"date": "2026-09-0%d" % d, "value": 40} for d in range(1, 8)]
        item = self.contributor({"status": "BALANCED", "value": 31}, series)
        self.assertIn("40", item["note"])

    def test_an_unknown_status_drops_the_contributor_rather_than_guessing(self):
        self.assertIsNone(self.contributor({"status": None, "value": 40}, []))


class ContributorTests(unittest.TestCase):
    def build(self, **overrides):
        wellness = {
            "sleep": {"hours": 6.9, "score": 82},
            "hrv": {"status": "BALANCED", "value": 48},
            "restingHr": {"value": 53},
        }
        wellness.update(overrides.pop("wellness", {}))
        effort = {"state": "within", "baseline": 52.0,
                  "days": [{"effort": 7.0}, {"effort": 12.0}]}
        effort.update(overrides.pop("effort", {}))
        return dashboard._recovery_metrics(
            nights({o: 7.0 for o in range(14)}),
            [{"date": "2026-09-08", "value": 48, "status": "BALANCED"}],
            [{"date": "2026-09-08", "value": 53}],
            wellness, effort, 45, TODAY)

    def test_builds_the_expected_contributor_set(self):
        keys = [item["key"] for item in self.build()["contributors"]]
        self.assertIn("restingHr", keys)
        self.assertIn("hrvBalance", keys)
        self.assertIn("sleepRegularity", keys)
        self.assertIn("activityBalance", keys)
        self.assertNotIn("bodyTemperature", keys)   # no Garmin equivalent

    def test_contributors_are_graded(self):
        grades = {item["key"]: item["grade"] for item in self.build()["contributors"]}
        self.assertEqual(grades["hrvBalance"], "optimal")
        self.assertIn(grades["sleep"], {"optimal", "good", "attention"})

    def test_resting_hr_above_baseline_is_penalised(self):
        good = dashboard._recovery_metrics(
            nights({o: 7.0 for o in range(14)}), [], [{"date": "2026-09-08", "value": 50}],
            {"restingHr": {"value": 50}}, {}, 45, TODAY)
        raised = dashboard._recovery_metrics(
            nights({o: 7.0 for o in range(14)}),
            [], [{"date": "2026-09-07", "value": 50}, {"date": "2026-09-08", "value": 60}],
            {"restingHr": {"value": 60}}, {}, 45, TODAY)
        by_key = lambda payload: {i["key"]: i["percent"] for i in payload["contributors"]}
        self.assertGreater(by_key(good)["restingHr"], by_key(raised)["restingHr"])

    def test_contributors_with_no_data_are_omitted_rather_than_shown_empty(self):
        payload = dashboard._recovery_metrics([], [], [], {}, {}, 45, TODAY)
        self.assertEqual(payload["contributors"], [])
        self.assertEqual(payload["sleepNeedHours"], 8.0)

    def test_reports_the_missing_metric_honestly(self):
        self.assertIn("temperature", self.build()["missing"].lower())


if __name__ == "__main__":
    unittest.main()
