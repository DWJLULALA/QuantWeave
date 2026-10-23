"""The page migration and trend-only job/API boundary."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class TrendAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        os.environ['STOCKLAB_RUN_ROOT']=cls.tmp.name
        cls.module=importlib.import_module('app')
        cls.run_root_patch=patch.object(cls.module,'RUN_ROOT',Path(cls.tmp.name))
        cls.run_root_patch.start()
        cls.client=cls.module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.run_root_patch.stop()
        cls.tmp.cleanup()

    def test_invalid_symbol_and_period_return_400(self):
        self.assertEqual(self.client.get('/api/trends/999999').status_code,400)
        self.assertEqual(self.client.get('/api/trends/000001?level=unsafe').status_code,400)

    def test_empty_snapshot_is_an_explicit_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(self.module,'ROOT',Path(tmp)):
            data=self.client.get('/api/trends').get_json()
            self.assertFalse(data['available'])

    def test_trend_refresh_is_allowlisted_and_deduplicated(self):
        self.module._jobs.clear()
        with patch.object(self.module,'run_command'):
            first=self.client.post('/api/jobs',json={'kind':'trend_refresh','params':{}})
            second=self.client.post('/api/jobs',json={'kind':'trend_refresh','params':{}})
        self.assertEqual(first.status_code,202)
        self.assertEqual(first.get_json()['id'],second.get_json()['id'])
        self.assertTrue(second.get_json()['reused'])
        self.module._jobs.clear()

    def test_sidebar_declares_separate_page_and_assets(self):
        html=self.client.get('/').get_data(as_text=True)
        self.assertIn('data-page="trends"',html)
        self.assertIn('id="page-trends"',html)
        self.assertIn('/static/trend-analysis.js',html)

    def test_factor_renderer_no_longer_contains_migrated_panels(self):
        source=(Path(__file__).parent/'static/app.js').read_text()
        factor=source[source.index('function renderFactors()'):source.index('function renderTrendLegacyPanels()')]
        for old_id in ['market-wave-output','chan-output','chan-multi-output']:
            self.assertNotIn(old_id,factor)


if __name__=='__main__':unittest.main()
