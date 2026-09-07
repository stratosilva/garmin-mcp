import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


if "starlette.responses" not in sys.modules:
    responses = types.ModuleType("starlette.responses")
    responses.HTMLResponse = type("HTMLResponse", (), {})
    responses.JSONResponse = type("JSONResponse", (), {})
    routing = types.ModuleType("starlette.routing")
    routing.Route = type("Route", (), {})
    starlette = types.ModuleType("starlette")
    sys.modules.setdefault("starlette", starlette)
    sys.modules["starlette.responses"] = responses
    sys.modules["starlette.routing"] = routing


MODULE_PATH = Path(__file__).parents[1] / "src" / "garmin_mcp" / "dashboard.py"
SPEC = importlib.util.spec_from_file_location("dashboard_strength_under_test", MODULE_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class FakeGarminClient:
    def __init__(self, payload):
        self.payload = payload

    def get_activity_exercise_sets(self, _activity_id):
        return self.payload


class StrengthDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.old_strength_path = os.environ.get("STRENGTH_LOG_PATH")
        self.old_recommendation_path = os.environ.get("RECOMMENDATION_CACHE_PATH")
        os.environ["STRENGTH_LOG_PATH"] = str(root / "strength.json")
        os.environ["RECOMMENDATION_CACHE_PATH"] = str(root / "recommendation.json")
        self.raw_payload = {
            "activityId": 123,
            "exerciseSets": [
                {
                    "messageIndex": 0,
                    "setType": "REST",
                    "duration": 40.0,
                    "exercises": [],
                },
                {
                    "messageIndex": 1,
                    "setType": "ACTIVE",
                    "duration": 32.4,
                    "startTime": "2026-09-02T18:32:00",
                    "repetitionCount": 24,
                    "weight": 20000.0,
                    "exercises": [
                        {
                            "category": "LUNGE",
                            "name": "DUMBBELL_BULGARIAN_SPLIT_SQUAT",
                            "probability": 100.0,
                        }
                    ],
                },
            ],
        }
        self.client = FakeGarminClient(self.raw_payload)

    def tearDown(self):
        if self.old_strength_path is None:
            os.environ.pop("STRENGTH_LOG_PATH", None)
        else:
            os.environ["STRENGTH_LOG_PATH"] = self.old_strength_path
        if self.old_recommendation_path is None:
            os.environ.pop("RECOMMENDATION_CACHE_PATH", None)
        else:
            os.environ["RECOMMENDATION_CACHE_PATH"] = self.old_recommendation_path
        self.temporary.cleanup()

    def test_normalises_only_non_rest_sets_and_converts_grams(self):
        rows = dashboard._normalise_garmin_strength_sets(self.raw_payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "garmin:1")
        self.assertEqual(rows[0]["exercise"], "Dumbbell Bulgarian Split Squat")
        self.assertEqual(rows[0]["reps"], 24)
        self.assertEqual(rows[0]["durationSeconds"], 32.4)
        self.assertEqual(rows[0]["weightKg"], 20.0)

    def test_custom_stability_exercise_is_available_in_the_catalog(self):
        self.assertIn("Split Stance Anti-Rotation Cable Lift", dashboard._strength_exercise_catalog())

    def test_weekly_muscle_volume_combines_strength_cardio_and_movement(self):
        strength = {"activities": [{
            "start": "2026-09-02T18:00:00",
            "sets": [
                {"exercise": "Chest Press", "setType": "ACTIVE"},
                {"exercise": "Split Stance Anti-Rotation Cable Lift", "setType": "ACTIVE"},
                {"exercise": "Chest Press", "setType": "WARMUP"},
            ],
        }]}
        activities = [
            {"date": "2026-09-01", "sport": "run", "name": "Easy run", "min": 40, "zones": [10, 20, 10, 0, 0]},
            {"date": "2026-08-27", "sport": "other", "typeKey": "indoor_rowing", "name": "Row machine", "min": 30},
        ]
        movement = [
            {"calendarDate": "2026-09-01", "totalSteps": 10000},
            {"calendarDate": "2026-08-25", "totalSteps": 8000},
        ]
        stats = [{"calendarDate": "2026-09-02", "floorsAscended": 12}]
        result = dashboard._muscle_volume_weeks(
            strength, activities, movement, stats, dashboard.datetime.date(2026, 9, 3), week_count=2,
        )

        self.assertEqual(len(result["weeks"]), 2)
        current = result["weeks"][-1]
        previous = result["weeks"][0]
        self.assertTrue(current["isCurrent"])
        current_muscles = {row["key"]: row for row in current["muscles"]}
        previous_muscles = {row["key"]: row for row in previous["muscles"]}
        self.assertEqual(current_muscles["chest"]["direct"], 1.0)
        self.assertEqual(current_muscles["core"]["direct"], 1.0)
        self.assertEqual(current_muscles["triceps"]["indirectStrength"], 0.5)
        self.assertGreater(current_muscles["quads"]["cardio"], 0)
        self.assertGreater(current_muscles["calves"]["movement"], 0)
        self.assertGreater(previous_muscles["back"]["cardio"], 0)
        chest = next(row for row in current["exerciseBreakdown"] if row["exercise"] == "Chest Press")
        chest_credits = {muscle["key"]: muscle["volumeCreditPct"] for muscle in chest["muscles"]}
        self.assertEqual(chest_credits["chest"], 100)
        self.assertEqual(chest_credits["triceps"], 50)
        self.assertEqual(chest_credits["delts"], 50)
        self.assertNotIn("biceps", chest_credits)

    def test_saves_overrides_and_manual_sets_without_mutating_garmin(self):
        loaded = dashboard._strength_activity_payload(self.client, 123)
        garmin_set = loaded["sets"][0]
        submission = {
            "activityName": "Strength",
            "activityStart": "2026-09-02T18:32:00",
            "sets": [
                {
                    **garmin_set,
                    "exercise": "Dumbbell Bulgarian Split Squat",
                    "reps": 12,
                    "weightKg": 20,
                    "perSide": True,
                },
                {
                    "id": "manual:calves:0",
                    "source": "manual",
                    "setType": "ACTIVE",
                    "exercise": "Standing Calf Raise",
                    "reps": 15,
                    "weightKg": 0,
                    "perSide": False,
                },
            ],
        }
        dashboard._save_strength_activity(123, submission)

        reloaded = dashboard._strength_activity_payload(self.client, 123)
        self.assertEqual(len(reloaded["sets"]), 2)
        self.assertEqual(reloaded["sets"][0]["reps"], 12)
        self.assertTrue(reloaded["sets"][0]["perSide"])
        self.assertEqual(reloaded["sets"][0]["rawReps"], 24)
        self.assertTrue(reloaded["sets"][0]["edited"])
        self.assertEqual(reloaded["sets"][1]["source"], "manual")

        summary = dashboard._strength_summary()
        self.assertEqual(summary["activities"][0]["workingSets"], 2)
        self.assertEqual(summary["activities"][0]["externalLoadVolumeKg"], 480.0)

    def test_server_snapshot_keeps_garmin_originals_immutable(self):
        loaded = dashboard._strength_activity_payload(self.client, 123)
        tampered = {
            **loaded["sets"][0],
            "reps": 12,
            "rawReps": 999,
            "durationSeconds": 45,
            "rawDurationSeconds": 999,
            "weightKg": 22,
            "rawWeightKg": 999,
            "perSide": True,
        }
        current = dashboard._normalise_garmin_strength_sets(self.raw_payload)
        dashboard._save_strength_activity(123, {"sets": [tampered]}, current)

        reloaded = dashboard._strength_activity_payload(self.client, 123)["sets"][0]
        self.assertEqual(reloaded["rawReps"], 24)
        self.assertEqual(reloaded["rawDurationSeconds"], 32.4)
        self.assertEqual(reloaded["rawWeightKg"], 20.0)
        self.assertEqual(reloaded["reps"], 12)
        self.assertEqual(reloaded["durationSeconds"], 45)
        self.assertEqual(reloaded["weightKg"], 22.0)

    def test_manual_sets_require_an_exercise_and_reps_or_duration(self):
        with self.assertRaisesRegex(ValueError, "manual sets need an exercise"):
            dashboard._save_strength_activity(123, {
                "sets": [{
                    "id": "manual:missing:0",
                    "source": "manual",
                    "exercise": None,
                    "reps": 12,
                    "weightKg": 0,
                }]
            })
        with self.assertRaisesRegex(ValueError, "manual sets need repetitions or a duration"):
            dashboard._save_strength_activity(123, {
                "sets": [{
                    "id": "manual:missing-effort:0",
                    "source": "manual",
                    "exercise": "Farmer's Walk",
                    "weightKg": 48,
                }]
            })

    def test_saves_timed_manual_sets_for_carries_and_holds(self):
        dashboard._save_strength_activity(123, {
            "activityName": "Carries and core",
            "activityStart": "2026-09-03T18:00:00",
            "sets": [{
                "id": f"manual:farmers:{index}",
                "source": "manual",
                "setType": "ACTIVE",
                "exercise": "Farmer's Walk",
                "reps": None,
                "durationSeconds": 45,
                "weightKg": 48,
                "perSide": False,
            } for index in range(3)] + [{
                "id": "manual:side-plank:0",
                "source": "manual",
                "setType": "ACTIVE",
                "exercise": "Side Plank",
                "reps": None,
                "durationSeconds": 30,
                "weightKg": None,
                "perSide": True,
            }],
        })

        activity = dashboard._strength_summary()["activities"][0]
        self.assertEqual(activity["workingSets"], 4)
        self.assertEqual(activity["timedSets"], 4)
        self.assertEqual(activity["timedSeconds"], 195.0)
        self.assertEqual(activity["sets"][0]["durationSeconds"], 45.0)
        self.assertEqual(activity["sets"][0]["weightKg"], 48.0)
        profile = dashboard._exercise_muscle_profile("Farmer's Walk")
        self.assertIn("core", {muscle["key"] for muscle in profile["muscles"]})

    def test_recommendation_snapshot_includes_entered_strength_and_calories(self):
        strength = {
            "activities": [{
                "name": "Strength",
                "workingSets": 4,
                "sets": [{"exercise": "Split Stance Anti-Rotation Cable Lift", "reps": 12}],
            }]
        }
        snapshot = dashboard._recommendation_snapshot({
            "date": "2026-09-02",
            "wellness": {
                "calories": {"total": 2400, "active": 500},
                "weight": {"kg": 74.2, "trend30": -0.3},
                "intensity": {"total": 180},
            },
            "strength": strength,
            "sleepSeries": [{"date": "2026-09-02", "score": 82}],
            "hrvSeries": [{"date": "2026-09-02", "value": 56, "status": "BALANCED"}],
            "trainingLoadTrend": [{"label": "Tue 02", "load": 40}],
            "fitnessSeries": [{"date": "2026-09-02", "fitness": 13.5, "form": 1.2}],
            "hrZonesWeek": [15, 70, 20, 0, 0],
            "muscleVolume": {"weeks": [{"label": "Aug 31–Sep 6", "muscles": [{"key": "core", "total": 4}]}]},
            "sports": {"run": {"week": {"sessions": 2}, "recent": [{"name": "Easy run"}]}},
            "recent": [
                {"sport": "run", "name": "Easy run", "min": 40},
                {"sport": "other", "typeKey": "elliptical", "name": "Elliptical", "min": 25},
                {"sport": "other", "name": "Strength", "min": 45},
            ],
        })
        self.assertEqual(snapshot["strength_training"], strength)
        self.assertEqual(snapshot["energy_and_body"]["activity_calories"]["active"], 500)
        self.assertEqual(snapshot["energy_and_body"]["garmin_weight"]["kg"], 74.2)
        self.assertEqual(snapshot["fitness"]["fitness_level_trend_42d"][-1]["fitness"], 13.5)
        self.assertEqual(snapshot["recovery"]["sleep_history_7d"][-1]["score"], 82)
        self.assertEqual(snapshot["fitness"]["training_load_trend_7d"][0]["load"], 40)
        self.assertEqual(snapshot["fitness"]["recent_cardio_workouts"][0]["name"], "Easy run")
        self.assertEqual(snapshot["fitness"]["recent_cardio_workouts"][1]["name"], "Elliptical")
        self.assertEqual(snapshot["fitness"]["cardio_by_sport"]["run"]["week"]["sessions"], 2)
        self.assertEqual(snapshot["muscle_stimulus_recent_weeks"][-1]["muscles"][0]["total"], 4)
        self.assertIn("Split Stance Anti-Rotation Cable Lift", str(snapshot["strength_training"]))
        self.assertIn("strength_training", dashboard._RECOMMENDATION_INSTRUCTIONS)
        self.assertIn("VO2", dashboard._RECOMMENDATION_INSTRUCTIONS)
        self.assertIn("exactly six numbered movement slots", dashboard._RECOMMENDATION_INSTRUCTIONS)
        self.assertIn("exactly two alternative exercises", dashboard._RECOMMENDATION_INSTRUCTIONS)
        self.assertIn("Romanian deadlift versus leg curl", dashboard._RECOMMENDATION_INSTRUCTIONS)
        self.assertIn("RECOVERY / MOBILITY", dashboard._RECOMMENDATION_INSTRUCTIONS)
        self.assertIn("Foam rolling", dashboard._RECOMMENDATION_INSTRUCTIONS)

    def test_recommendation_snapshot_compares_previous_day_load_with_today_pain(self):
        snapshot = dashboard._recommendation_snapshot({
            "date": "2026-09-03",
            "injuries": {"records": [
                {"date": "2026-09-02", "left_big_toe_strain": 0,
                 "left_foot_plantar_fasciitis": 2, "right_knee_patellar_tendon": 3},
                {"date": "2026-09-03", "left_big_toe_strain": 0,
                 "left_foot_plantar_fasciitis": 2, "right_knee_patellar_tendon": 2},
            ]},
            "recent": [{"date": "2026-09-02", "sport": "run", "name": "Easy run", "min": 30, "km": 5}],
            "strength": {"activities": [{"start": "2026-09-02T18:00:00", "name": "Calf raises",
                                              "workingSets": 3, "sets": [{"exercise": "Standing Calf Raise"}]}]},
        })
        response = snapshot["prior_day_load_and_today_pain"]
        self.assertEqual(response["previous_day_activities"][0]["sport"], "run")
        self.assertEqual(response["previous_day_strength"][0]["workingSets"], 3)
        self.assertEqual(response["pain_change_next_day"]["right_knee_patellar_tendon"], -1)
        self.assertIn("not automatically", dashboard._RECOMMENDATION_INSTRUCTIONS)

    def test_recommendation_cache_changes_when_the_prompt_changes(self):
        saved = dashboard._save_recommendation("2026-09-03", {"text": "Plan", "model": "test-model"})
        self.assertEqual(saved["promptVersion"], dashboard._RECOMMENDATION_PROMPT_VERSION)
        self.assertEqual(dashboard._cached_recommendation("2026-09-03")["text"], "Plan")

        cache_path = Path(os.environ["RECOMMENDATION_CACHE_PATH"])
        old = json.loads(cache_path.read_text(encoding="utf-8"))
        old["promptVersion"] = dashboard._RECOMMENDATION_PROMPT_VERSION - 1
        cache_path.write_text(json.dumps(old), encoding="utf-8")
        self.assertIsNone(dashboard._cached_recommendation("2026-09-03"))


if __name__ == "__main__":
    unittest.main()
