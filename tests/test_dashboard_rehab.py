import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_dashboard_strength import dashboard


class RehabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'injuries.csv'
        self.addCleanup(patch.stopall)
        patch.object(dashboard, '_injury_measurements_path', return_value=self.path).start()
        patch.object(dashboard, '_dashboard_database', return_value=None).start()
        self.today = datetime.date.today().isoformat()
        self.injury = dashboard._INJURY_FIELDS[1]

    def session(self, **changes):
        return dict(kind='session', id='session1', date=self.today, routineId='starter', difficulty='usual', **changes)

    def test_default_is_only_a_template(self):
        data = dashboard._rehab_data()
        self.assertEqual(data['entries'], [])
        self.assertEqual(len(data['routines'][0]['exercises']), 4)
        self.assertTrue(all(not x['dose'] for x in data['routines'][0]['exercises']))

    def test_session_snapshot_survives_routine_edit_and_disable(self):
        dashboard._save_rehab(self.session(notes='Comfortable'))
        routine = dashboard._rehab_data()['routines'][0]
        routine.update(kind='routine', name='Changed', enabled=False)
        routine['exercises'][0]['dose'] = '3 x 30 seconds'
        dashboard._save_rehab(routine)
        row = dashboard._rehab_data()['entries'][0]
        self.assertEqual(row['routine']['name'], 'My knee & foot rehab')
        self.assertEqual(row['routine']['exercises'][0]['dose'], '')
        dashboard._save_rehab(self.session(notes='Edited'))
        self.assertEqual(dashboard._rehab_data()['entries'][0]['routine']['name'], 'My knee & foot rehab')

    def test_repeat_quick_click_preserves_notes(self):
        dashboard._save_rehab(self.session(notes='Keep this'))
        dashboard._save_rehab(self.session(notes='', quick=True))
        self.assertEqual(dashboard._rehab_data()['entries'][0]['notes'], 'Keep this')
        duplicate = self.session(); duplicate['id'] = 'another'
        with self.assertRaises(ValueError): dashboard._save_rehab(duplicate)

    def test_optional_checkin_and_delete(self):
        entry = dict(kind='checkin', id='check1', date=self.today, injuryId=self.injury, timing='morning', stiffness='12')
        dashboard._save_rehab(entry)
        row = dashboard._rehab_data()['entries'][0]
        self.assertIsNone(row['pain'])
        self.assertEqual(row['stiffness'], 12)
        dashboard._save_rehab(dict(kind='checkin', id='check1', action='delete'))
        self.assertEqual(dashboard._rehab_data()['entries'], [])

    def test_reject_bad_fields_without_writing(self):
        base = dict(kind='checkin', date=self.today, injuryId=self.injury, timing='morning', pain=0)
        for update in [{'pain': 11}, {'pain': True}, {'stiffness': -2}, {'injuryId': 'bad'}, {'timing':'later'}, {'date':'2099-01-01'}, {'pain':'NaN'}, {'function':'bad'}]:
            with self.subTest(update=update), self.assertRaises(ValueError): dashboard._save_rehab(base | update)
        self.assertEqual(dashboard._rehab_data()['entries'], [])
        dashboard._save_rehab(base)
        self.assertEqual(dashboard._rehab_data()['entries'][0]['pain'], 0)

    def test_new_routine_keeps_starter(self):
        dashboard._save_rehab(dict(kind='routine', name='Another', exercises=[dict(name='Exercise', dose='2 sets')]))
        self.assertEqual(len(dashboard._rehab_data()['routines']), 2)

    def test_database_persistence_path(self):
        db=Mock();db.rehab_records.return_value={};db.injury_definitions.return_value=dashboard._INJURY_DEFAULTS
        with patch.object(dashboard, '_dashboard_database', return_value=db):
            dashboard._save_rehab(self.session())
        key,value=db.save_rehab_record.call_args.args
        self.assertEqual(key,'entry:session1')
        self.assertEqual(value['routineId'],'starter')
        self.assertFalse(self.path.with_suffix('.rehab.json').exists())

    def test_advice_only_includes_month(self):
        past=(datetime.date.today()-datetime.timedelta(days=40)).isoformat()
        data={'date':self.today,'rehab':{'entries':[{'date':past,'notes':'old'},{'date':self.today,'notes':'today'}]}}
        result=dashboard._recommendation_snapshot(data)
        self.assertEqual(result['rehab_and_symptom_checkins'],[{'date':self.today,'notes':'today'}])

    def test_shipped_html_includes_ui(self):
        self.assertIn('function renderRehab(', dashboard.PAGE_HTML)
        self.assertIn('function rehabComparison(', dashboard.PAGE_HTML)
