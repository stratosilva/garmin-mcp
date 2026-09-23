import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_dashboard_sleep import dashboard, nights, TODAY


class WeeklySleepTests(unittest.TestCase):
    def test_worked_example_and_no_interest(self):
        hours=[6.5,7,5.5,6,6.5,9,8.5]
        debt=dashboard._sleep_debt(nights(dict(enumerate(hours))),8,TODAY)
        self.assertEqual(debt['minutes'],510)
        self.assertEqual(debt['recordedDays'],7)
        constant=dashboard._sleep_debt(nights({i:6.5 for i in range(60)}),7.5,TODAY)
        self.assertTrue(all(p['minutes']==420 for p in constant['series']))

    def test_naps_and_correction_replace_rather_than_add(self):
        entries={TODAY.isoformat():{'nightHours':7,'napMinutes':20,'notes':'Late caffeine'}}
        debt=dashboard._sleep_debt(nights({0:5}),7.5,TODAY,entries)
        self.assertEqual(debt['minutes'],10)
        self.assertEqual(debt['days'][-1]['source'],'manual')
        self.assertEqual(debt['days'][-1]['notes'],'Late caffeine')
        entries[TODAY.isoformat()]['napMinutes']=60
        self.assertEqual(dashboard._sleep_debt(nights({0:5}),7.5,TODAY,entries)['minutes'],0)

    def test_unknown_night_with_nap_remains_unknown_and_zero_is_valid(self):
        entries={TODAY.isoformat():{'napMinutes':20}}
        debt=dashboard._sleep_debt([],7.5,TODAY,entries)
        self.assertIsNone(debt['minutes'])
        self.assertEqual(debt['missingDays'],7)
        entries[TODAY.isoformat()]['nightHours']=0
        debt=dashboard._sleep_debt([],7.5,TODAY,entries)
        self.assertEqual(debt['minutes'],430)
        self.assertEqual(debt['recordedDays'],1)

    def test_precise_seconds_are_used(self):
        row=nights({0:7.5})[0];row['seconds']=7.5*3600-120
        self.assertEqual(dashboard._sleep_debt([row],7.5,TODAY)['minutes'],2)


class SleepPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'DATABASE_URL':'','SLEEP_LOG_PATH':str(Path(self.temp.name)/'sleep.json'),'RECOMMENDATION_CACHE_PATH':str(Path(self.temp.name)/'advice.json')})
        env.start();self.addCleanup(env.stop)

    def test_default_save_edit_clear_and_preserve_other_days(self):
        self.assertEqual(dashboard._sleep_log(),{'targetHours':7.5,'entries':{}})
        dashboard._save_sleep_log({'targetHours':7.7},TODAY)
        payload={'date':TODAY.isoformat(),'napMinutes':25,'notes':'Recovery'}
        dashboard._save_sleep_log(payload,TODAY)
        dashboard._save_sleep_log(payload,TODAY)
        self.assertEqual(len(dashboard._sleep_log()['entries']),1)
        dashboard._save_sleep_log({'date':(TODAY-datetime.timedelta(days=1)).isoformat(),'napMinutes':15},TODAY)
        result=dashboard._save_sleep_log({'date':TODAY.isoformat(),'napMinutes':0,'nightHours':'','notes':''},TODAY)
        self.assertEqual(result['targetHours'],7.7)
        self.assertEqual(len(result['entries']),2)
        self.assertIsNone(result['entries'][TODAY.isoformat()]['nightHours'])

    def test_invalid_entries_do_not_write(self):
        invalid=[{'targetHours':'nan'},{'targetHours':True},{'targetHours':20},{'date':'bad'},
                 {'date':(TODAY+datetime.timedelta(days=1)).isoformat()},
                 {'date':TODAY.isoformat(),'napMinutes':-2},
                 {'date':TODAY.isoformat(),'napMinutes':1.5},
                 {'date':TODAY.isoformat(),'nightHours':24,'napMinutes':30},
                 {'date':TODAY.isoformat(),'notes':'x'*2001}]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:80]),self.assertRaises(ValueError):
                dashboard._save_sleep_log(payload,TODAY)
        self.assertFalse(dashboard._sleep_log_path().exists())

    def test_saved_target_flows_to_recovery(self):
        log=dashboard._save_sleep_log({'targetHours':7.2,'date':TODAY.isoformat(),'napMinutes':12},TODAY)
        result=dashboard._recovery_metrics(nights({0:7}),[],[],{}, {},45,TODAY,log)
        self.assertEqual(result['sleepNeedHours'],7.2)
        self.assertEqual(result['debt']['minutes'],0)

    def test_database_path_is_used_when_available(self):
        from unittest.mock import Mock
        db=Mock();db.sleep_log.return_value={'targetHours':7.5,'entries':{}}
        with patch.object(dashboard,'_dashboard_database',return_value=db):
            dashboard._save_sleep_log({'date':TODAY.isoformat(),'napMinutes':30},TODAY)
        db.save_sleep_log.assert_called_once_with(None,TODAY.isoformat(),{'napMinutes':30,'nightHours':None,'notes':''})
        self.assertFalse(dashboard._sleep_log_path().exists())

    def test_route_save_read_and_validation_without_garmin(self):
        import asyncio
        from types import SimpleNamespace
        class Response:
            def __init__(self, data, status_code=200):
                self.data=data;self.status_code=status_code
        routes=[]
        def route(path, handler, methods):
            return SimpleNamespace(path=path, handler=handler, methods=methods)
        class Request:
            def __init__(self,method,body=None):self.method=method;self.body=body
            async def json(self):return self.body
        with patch.object(dashboard,'Route',route),patch.object(dashboard,'JSONResponse',Response):
            dashboard.add_dashboard_routes(SimpleNamespace(router=SimpleNamespace(routes=routes)),None)
            endpoint=next(r for r in routes if r.path=='/api/sleep-log')
            saved=asyncio.run(endpoint.handler(Request('POST',{'targetHours':7.6})))
            self.assertEqual(saved.status_code,200)
            read=asyncio.run(endpoint.handler(Request('GET')))
            self.assertEqual(read.data['targetHours'],7.6)
            invalid=asyncio.run(endpoint.handler(Request('POST',{'targetHours':0})))
            self.assertEqual(invalid.status_code,400)


if __name__=='__main__':unittest.main()
