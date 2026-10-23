"""GPU scheduler regression tests; run in the deployed Kronos Python runtime.

CUDA interactions and model inference are mocked; no GPU memory is allocated.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import torch
import engine_intraday as engine

GIB = 1024**3


class IntradayGpuTests(unittest.TestCase):
    def test_shared_gpu_is_eligible_and_visible_indices_are_used(self):
        # Logical cuda:0 may refer to physical GPU 5. Only torch visibility
        # and free memory determine selection, never utilization percentage.
        with patch.object(torch.cuda, 'is_available', return_value=True), \
             patch.object(torch.cuda, 'device_count', return_value=3), \
             patch.object(torch.cuda, 'mem_get_info', side_effect=lambda i: [(30*GIB,80*GIB), (6*GIB,80*GIB), (28*GIB,80*GIB)][i]):
            self.assertEqual(engine._auto_device(), 'cuda:0')

    def test_budget_and_batch_on_30gib_free_card(self):
        with patch.object(torch.cuda, 'mem_get_info', return_value=(30*GIB,80*GIB)), \
             patch.object(torch.cuda, 'set_per_process_memory_fraction') as fraction:
            self.assertEqual(engine._recommended_batch_size(torch.device('cuda:0'),1024),512)
            self.assertAlmostEqual(fraction.call_args.args[0], 24/80)

    def test_insufficient_memory_is_not_silently_accepted(self):
        with patch.object(torch.cuda,'is_available',return_value=True), \
             patch.object(torch.cuda,'device_count',return_value=1), \
             patch.object(torch.cuda,'mem_get_info',return_value=(6*GIB,80*GIB)):
            with self.assertRaisesRegex(RuntimeError, '16GiB'):
                engine._auto_device()

    def test_oom_retries_same_records_without_duplicates(self):
        records = [{'ts_code':f'{i:06}.SZ', 'last_close':10., 'context_end_date':'2026-09-14T15:00:00'} for i in range(7)]
        batches = []
        def simulate(*args):
            batch = args[2]
            batches.append([r['ts_code'] for r in batch])
            if len(batches)==1:
                raise torch.cuda.OutOfMemoryError('simulated allocation race')
            return np.full((len(batch),16),10.5)
        with tempfile.TemporaryDirectory() as output, \
             patch.object(engine,'set_seed'), \
             patch.object(engine.pd,'read_parquet',return_value=pd.DataFrame({'timestamps':[pd.Timestamp('2026-09-14 15:00')]})), \
             patch.object(engine,'_records_latest',return_value=records), \
             patch.object(engine,'_next_weekday',return_value=pd.Timestamp('2026-09-15')), \
             patch.object(engine,'_recommended_batch_size',return_value=4), \
             patch.object(engine,'load_model',return_value=(None,None)), \
             patch.object(engine,'_forecast_batch',side_effect=simulate), \
             patch.object(torch.cuda,'empty_cache'), \
             patch.object(torch.cuda,'reset_peak_memory_stats'), \
             patch.object(torch.cuda,'max_memory_allocated',return_value=0), \
             patch.object(torch.cuda,'max_memory_reserved',return_value=0):
            manifest=engine.run_intraday_prediction(output,panel_path='test.parquet',stock_basic_path=None,device='cuda:0',calendar_dir=None)
            rows=pd.read_csv(Path(output)/'intraday_next_session_predictions.csv')
            self.assertEqual(manifest['prediction_count'],7)
            self.assertEqual(manifest['oom_retries'],1)
            self.assertEqual(rows.ts_code.nunique(),7)
            self.assertEqual(batches[1],batches[0][:2])
            self.assertEqual([len(b) for b in batches],[4,2,2,2,1])


if __name__ == '__main__':
    unittest.main()
