import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from test_dashboard_strength import dashboard


class ManualWorkoutDeletionTests(unittest.TestCase):
    def test_file_deletion_preserves_other_entries_and_garmin_corrections(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'STRENGTH_LOG_PATH': str(Path(directory)/'strength.json')}), patch.object(dashboard, '_dashboard_database', return_value=None):
            store={'activities':{'123':{'sets':[]}},'manualWorkouts':{'one':{'sets':[]},'two':{'sets':[]}},'manualActivities':{'run':{'sport':'run'}}}
            dashboard._write_strength_log_unlocked(store)
            self.assertTrue(dashboard._delete_manual_activity('one','strength'))
            saved=dashboard._read_strength_log_unlocked()
            self.assertEqual(saved['manualWorkouts'],{'two':{'sets':[]}})
            self.assertEqual(saved['activities'],store['activities'])
            self.assertEqual(saved['manualActivities'],store['manualActivities'])
            self.assertFalse(dashboard._delete_manual_activity('one','strength'))
            self.assertTrue(dashboard._delete_manual_activity('run','endurance'))
            self.assertEqual(dashboard._read_strength_log_unlocked()['manualActivities'],{})

    def test_database_deletion_is_scoped_and_parameterized(self):
        path=Path(__file__).parents[1]/'src/garmin_mcp/dashboard_storage.py'
        spec=importlib.util.spec_from_file_location('manual_storage_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        db=module.DashboardDatabase('unused');connection=MagicMock();cursor=connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.rowcount=1
        with patch.object(db,'ensure_schema'),patch.object(db,'_connect',return_value=connection):
            self.assertTrue(db.delete_manual_activity("manual-'quoted",'strength'))
            sql,args=cursor.execute.call_args.args
            self.assertEqual(sql,'DELETE FROM dashboard_manual_strength_workouts WHERE manual_id = %s')
            self.assertEqual(args,("manual-'quoted",))
            self.assertTrue(db.delete_manual_activity('run','endurance'))
            self.assertIn('dashboard_manual_endurance_activities',cursor.execute.call_args.args[0])
            cursor.rowcount=0
            self.assertFalse(db.delete_manual_activity('missing','strength'))
            with self.assertRaises(ValueError):db.delete_manual_activity('123','garmin')

    def test_dashboard_routes_database_deletion(self):
        db=MagicMock();db.delete_manual_activity.return_value=True
        with patch.object(dashboard,'_dashboard_database',return_value=db):
            self.assertTrue(dashboard._delete_manual_activity('manual-one','strength'))
        db.delete_manual_activity.assert_called_once_with('manual-one','strength')
