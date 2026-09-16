import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from test_dashboard_strength import dashboard


class InjurySettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "injuries.csv"
        self.addCleanup(patch.stopall)
        patch.object(dashboard, "_injury_measurements_path", return_value=self.path).start()
        self.database = patch.object(dashboard, "_dashboard_database", return_value=None).start()
        self.today = datetime.date.today().isoformat()
        self.fields = dashboard._INJURY_FIELDS

    def save(self, definitions):
        return dashboard._save_injury_definitions({"definitions": definitions})

    def test_disable_edit_reenable_preserves_legacy_scores(self):
        self.path.write_text("date," + ",".join(self.fields) + "\n" + self.today + ",8,2,3\n")
        definitions = dashboard._injury_definitions()
        definitions[0].update(enabled=False, name="Recovered toe", color="#abcdef")
        self.save(definitions)
        dashboard._append_injury_measurement({"date": self.today, self.fields[1]: 4, self.fields[2]: 5})
        records = dashboard._injury_measurements()["records"]
        self.assertEqual(records[-1][self.fields[0]], 8)
        self.assertEqual(records[-1][self.fields[1]], 4)
        definitions[0]["enabled"] = True
        self.save(definitions)
        result = dashboard._injury_measurements()
        self.assertEqual(result["definitions"][0]["name"], "Recovered toe")
        self.assertEqual(result["records"][-1][self.fields[0]], 8)

    def test_new_injury_has_missing_history_and_can_receive_scores(self):
        dashboard._append_injury_measurement(dict.fromkeys(self.fields, 2))
        definitions = dashboard._injury_definitions()
        definitions.append({"name": "Shoulder", "color": "#123456", "enabled": True})
        saved = self.save(definitions)
        new_id = saved[-1]["id"]
        self.assertTrue(new_id.startswith("injury_"))
        self.assertIsNone(dashboard._injury_measurements()["records"][-1].get(new_id))
        dashboard._append_injury_measurement({**dict.fromkeys(self.fields, 3), new_id: 6})
        self.assertEqual(dashboard._injury_measurements()["records"][-1][new_id], 6)

    def test_settings_validation_does_not_change_saved_settings(self):
        for mutate in [lambda rows: rows.pop(), lambda rows: rows[0].update(color="red"),
                       lambda rows: rows[0].update(name=rows[1]["name"].upper()),
                       lambda rows: rows[0].update(id="unknown")]:
            rows = dashboard._injury_definitions()
            mutate(rows)
            with self.assertRaises(ValueError):
                self.save(rows)
        self.assertEqual(dashboard._injury_definitions(), dashboard._INJURY_DEFAULTS)

    def test_disabled_fields_and_invalid_scores_cannot_overwrite_history(self):
        rows = dashboard._injury_definitions()
        rows[0]["enabled"] = False
        self.save(rows)
        with self.assertRaises(ValueError):
            dashboard._append_injury_measurement(dict.fromkeys(self.fields, 1))
        for value in [-1, 11, 1.5, "nan", "inf"]:
            with self.assertRaises(ValueError):
                dashboard._append_injury_measurement({self.fields[1]: value, self.fields[2]: 1})
        for row in rows:
            row["enabled"] = False
        self.save(rows)
        with self.assertRaises(ValueError):
            dashboard._append_injury_measurement({})

    def test_database_overlays_legacy_without_losing_other_scores(self):
        db = Mock()
        self.database.return_value = db
        db.injury_definitions.return_value = None
        date = datetime.date.today()
        db.injury_measurements.return_value = [(date, 8, 2, 3)]
        db.injury_scores.return_value = [(date, {self.fields[1]: 5})]
        row = dashboard._injury_measurements()["records"][-1]
        self.assertEqual([row[key] for key in self.fields], [8, 5, 3])
        dashboard._append_injury_measurement(dict.fromkeys(self.fields, 4))
        db.upsert_injury_scores.assert_called_once_with(date, dict.fromkeys(self.fields, 4))

    def test_recommendations_exclude_disabled_scores(self):
        definitions = dashboard._injury_definitions()
        definitions[0]["enabled"] = False
        context = dashboard._recommendation_snapshot({"date": self.today, "injuries": {
            "definitions": definitions, "records": [{"date": self.today, **dict.fromkeys(self.fields, 5)}]}})
        self.assertNotIn(self.fields[0], context["pain_latest"])
        self.assertEqual(len(context["tracked_injuries"]), 2)


if __name__ == "__main__":
    unittest.main()
