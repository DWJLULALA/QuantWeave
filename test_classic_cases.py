"""Causal volume features and historical teaching API contracts."""
from pathlib import Path
import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from classic_cases import volume_features


class ClassicCases(unittest.TestCase):
    def test_volume_baselines_exclude_today_and_never_read_future(self):
        v=np.arange(1,61,dtype=float)
        b=pd.DataFrame({'open':v,'close':v,'high':v+1,'low':v-.5,'volume':v*100,'amount':v*1000})
        f=volume_features(b)
        self.assertAlmostEqual(f.amount_ratio20.iloc[20],21/10.5)
        pd.testing.assert_frame_equal(volume_features(b.iloc[:40]),f.iloc[:40])
        future=b.copy();future.loc[40:,['volume','amount']]*=1000
        pd.testing.assert_frame_equal(volume_features(future).iloc[:40],f.iloc[:40])

    def test_classics_route_has_explicit_empty_state_and_serves_versioned_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.setdefault('STOCKLAB_RUN_ROOT',str(Path(tmp)/'runs'))
            app=importlib.import_module('app')
            with patch.object(app,'ROOT',Path(tmp)):
                client=app.app.test_client()
                self.assertEqual(client.get('/api/chan/classic-cases').status_code,503)
                path=Path(tmp)/'data/chan_classic/cases.json';path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'available':True,'rule_version':'fixture','cases':[]}))
                self.assertEqual(client.get('/api/chan/classic-cases').get_json()['rule_version'],'fixture')

    def test_classic_page_is_in_chan_navigation(self):
        html=(Path(__file__).parent/'templates/index.html').read_text()
        for text in ('缠论','经典案例分析','data-page="chan_cases"','id="page-chan_cases"','/static/chan-cases.js'):
            self.assertIn(text,html)

if __name__=='__main__':unittest.main()
