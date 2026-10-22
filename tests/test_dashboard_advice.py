import datetime
import json
import unittest
from unittest.mock import patch, Mock

from test_dashboard_sleep import dashboard


class AdviceHistoryTests(unittest.TestCase):
    def test_treadmill_threshold_and_real_report(self):
        for hr, expected in [(109, "walk"), (110, "run"), (119, "run"), (120, "run")]:
            with self.subTest(hr=hr):
                row = dashboard._map_activity({"activityType": {"typeKey": "treadmill_running"},
                    "averageHR": hr, "duration": 960, "distance": 2070})
                self.assertEqual(row["sport"], expected)
                self.assertEqual(row["min"], 16)
        walk = dashboard._map_activity({"activityType": {"typeKey": "walking"}, "averageHR": 119})
        self.assertEqual(walk["sport"], "walk")

    def test_full_calendar_month_preserves_notes_and_more_than_twelve_activities(self):
        today = datetime.date(2026, 10, 22)
        rows = [{"date": (today - datetime.timedelta(days=i)).isoformat(),
                 "sport": "run", "min": 15} for i in range(31)]
        data = {"date": today.isoformat(), "recent": rows[:12], "adviceHistory": {"activities": rows},
                "injuries": {"records": [dict(row, notes="Foot settles by morning", left_foot_plantar_fasciitis=2) for row in rows]},
                "sleepSeries": [dict(row, hours=7.5) for row in rows], "hrvSeries": rows, "rhrSeries": rows}
        result = dashboard._recommendation_snapshot(data)
        self.assertEqual(len(result["fitness"]["recent_cardio_workouts"]), 30)
        self.assertEqual(len(result["pain_trend_30d"]), 30)
        self.assertEqual(result["pain_trend_30d"][0]["date"], "2026-09-23")
        self.assertEqual(result["pain_latest"]["date"], "2026-10-22")
        self.assertEqual(result["pain_latest"]["notes"], "Foot settles by morning")
        for key in ("sleep_history_30d", "hrv_history_30d", "resting_hr_history_30d"):
            self.assertEqual(len(result["recovery"][key]), 30)

    def test_strength_history_not_limited_to_ui_eight(self):
        sessions = [{"start": f"2026-10-{i:02d}T10:00:00", "sets": [
            {"exercise": "Machine Chest Press", "weightKg": 70 if i == 22 else 52,
             "reps": 5 if i == 22 else 8, "perSide": False}]} for i in range(1, 23)]
        result = dashboard._recommendation_snapshot({"date": "2026-10-22", "strength": {"activities": sessions[-8:]},
            "adviceHistory": {"strength": sessions}})
        self.assertEqual(len(result["strength_training"]["activities"]), 22)
        press = result["strength_progression"][0]
        self.assertEqual(press["sessions"][-1]["sets"][0]["weightKg"], 70)
        self.assertEqual(press["sessions"][-1]["sets"][0]["reps"], 5)

    def test_equipment_and_per_side_are_not_interchangeable(self):
        result = dashboard._strength_progression([{"start": "2026-10-22", "sets": [
            {"exercise": "Machine Chest Press", "weightKg": 70, "reps": 5},
            {"exercise": "Incline Barbell Bench Press", "weightKg": 30, "reps": 8},
            {"exercise": "Machine Chest Press", "perSide": True, "weightKg": 35, "reps": 8},
            {"exercise": "Machine Chest Press", "weightKg": 20, "reps": 10, "setType": "warmup"}]}])
        self.assertEqual(len(result), 3)
        self.assertEqual(len(result[0]["sessions"][0]["sets"]), 1)

    def test_running_cap_uses_completed_week_and_partial_is_marked(self):
        rows = [{"date": f"2026-10-{i:02d}", "sport": "run", "min": 15} for i in range(12, 19)]
        rows += [{"date": "2026-10-22", "sport": "run", "min": 16},
                 {"date": "2026-10-22", "sport": "walk", "min": 60}]
        result = dashboard._running_progression(rows, "2026-10-22")
        self.assertEqual(result["current_week_ceiling_minutes"], 115.5)
        self.assertEqual(result["remaining_under_ceiling_minutes"], 99.5)
        self.assertFalse(result["weeks"][-1]["complete"])
        self.assertEqual(result["weeks"][-1]["minutes"], 16)
        self.assertIsNone(dashboard._running_progression([], "2026-10-22")["current_week_ceiling_minutes"])

    def test_missing_or_future_dates_excluded(self):
        self.assertEqual(dashboard._advice_month([{}, {"date": "2026-10-23"}, {"date": "2026-09-22"}], "2026-10-22"), [])

    def test_browser_sends_month_context_and_resting_hr(self):
        self.assertIn("adviceHistory:d.adviceHistory", dashboard.PAGE_HTML)
        self.assertIn("rhrSeries:d.rhrSeries", dashboard.PAGE_HTML)

    def test_api_receives_notes_and_rejects_truncated_advice(self):
        response = Mock(ok=True)
        response.json.return_value = {"status": "completed", "output_text": "Plan"}
        data = {"date": "2026-10-22", "injuries": {"records": [
            {"date": "2026-10-22", "notes": "Tender after stairs"}]}}
        with patch.dict(dashboard.os.environ, {"OPENAI_API_KEY": "test"}), patch.object(dashboard.requests, "post", return_value=response) as post:
            self.assertEqual(dashboard._openai_recommendation(data)["text"], "Plan")
            sent = json.loads(post.call_args.kwargs["json"]["input"])
            self.assertEqual(sent["pain_trend_30d"][0]["notes"], "Tender after stairs")
            response.json.return_value = {"status": "incomplete", "output_text": "Partial"}
            with self.assertRaisesRegex(RuntimeError, "cut short"):
                dashboard._openai_recommendation(data)


if __name__ == "__main__":
    unittest.main()
