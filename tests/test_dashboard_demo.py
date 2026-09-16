"""Test the public-data boundary without importing or logging in to Garmin."""
import ast
import asyncio
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).parents[1]/'src/garmin_mcp'
PKG='_public_demo_test'
package=types.ModuleType(PKG);package.__path__=[str(ROOT)];sys.modules[PKG]=package
dashboard=types.ModuleType(PKG+'.dashboard')
tree=ast.parse((ROOT/'dashboard.py').read_text(encoding='utf-8'))
dashboard.PAGE_HTML=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PAGE_HTML' for t in n.targets))
sys.modules[PKG+'.dashboard']=dashboard
from _public_demo_test.demo import demo_html
from _public_demo_test.demo_data import demo_data
from _public_demo_test.auth import BearerAuthMiddleware


class PublicDemoTests(unittest.TestCase):
    def test_synthetic_data_is_repeatable_and_independent(self):
        today=datetime.date(2026,9,17)
        one,two=demo_data(today),demo_data(today)
        self.assertEqual(one,two)
        one['injuries']['records'][-1]['notes']='changed'
        self.assertNotEqual(one,two)
        self.assertEqual(two['name'],'Miles Ahead')
        self.assertEqual(len(two['fitnessSeries']),732)
        for week in two['relativeEffort']['weeks']:
            self.assertEqual(week['effort'],sum(x['effort'] or 0 for x in week['days']))

    def test_page_contains_attribution_and_no_token_parameter(self):
        page=demo_html()
        self.assertIn('Manuel Silva Gallego',page)
        self.assertIn('Built with ChatGPT',page)
        self.assertIn('Strava-inspired',page)
        self.assertIn('Miles Ahead',page)
        self.assertNotIn('new URLSearchParams(location.search).get("token")',page)
        self.assertIn('connect-src', (ROOT/'demo.py').read_text())
        self.assertLess(page.index('id="demo-story"'),page.index('// All demo edits'))

    def test_only_exact_demo_path_is_added_to_auth_exemptions(self):
        tree=ast.parse((ROOT/'http_server.py').read_text())
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='BearerAuthMiddleware']
        paths=next(ast.literal_eval(k.value) for k in calls[0].keywords if k.arg=='exempt_paths')
        self.assertEqual(set(paths),{'/healthz','/favicon.ico','/demo'})
        async def check(path):
            events=[]
            async def app(scope,receive,send):
                await send({'type':'http.response.start','status':200,'headers':[]})
            async def send(event): events.append(event)
            middleware=BearerAuthMiddleware(app,token='test-token-at-least-16-chars',exempt_paths=paths)
            await middleware({'type':'http','path':path,'headers':[],'query_string':b''},None,send)
            return events[0]['status']
        self.assertEqual(asyncio.run(check('/demo')),200)
        for path in ['/dashboard','/api/dashboard','/api/injury-settings','/mcp','/demo/api/dashboard','/demo/']:
            self.assertEqual(asyncio.run(check(path)),401,path)


if __name__=='__main__': unittest.main()
