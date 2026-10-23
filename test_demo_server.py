"""Smoke tests for the zero-data demo server.

Run with::

    python -m unittest test_demo_server -v
"""

from __future__ import annotations

import time
import unittest

import demo_server


class DemoServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        demo_server.app.config.update(TESTING=True)
        cls.client = demo_server.app.test_client()

    def get_json(self, path: str):
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200, f"{path} -> {res.status_code}")
        return res.get_json()

    def test_index(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("QuantWeave", res.get_data(as_text=True))

    def test_status_is_marked_demo(self):
        data = self.get_json("/api/status")
        self.assertTrue(data["demo"])
        self.assertTrue(data["capabilities"]["predict"]["available"])

    def test_trends_and_detail(self):
        trends = self.get_json("/api/trends")
        self.assertTrue(trends["available"])
        self.assertEqual(len(trends["classifications"]["sw1"]["industries"]), 31)
        detail = self.get_json("/api/trends/801780?level=D")
        self.assertTrue(detail["available"])
        self.assertTrue(detail["chart"]["bars"])

    def test_champion_workbook_trimmed(self):
        wb = self.get_json("/api/champion_workbook")
        rows = wb["sheets"]["个股最新"]["rows"]
        self.assertGreater(len(rows), 100)
        self.assertLessEqual(len(rows), 320)

    def test_artifacts_are_downloadable(self):
        arts = self.get_json("/api/artifacts")
        self.assertGreaterEqual(len(arts), 8)
        res = self.client.get("/api/artifacts/factor_daily.csv")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"signal_date", res.data)

    def test_paper_account(self):
        data = self.get_json("/api/intraday/account")
        account = data["account"]
        self.assertEqual(len(account["positions"]), 5)
        self.assertAlmostEqual(account["total_asset"], account["cash"] + account["market_value"], places=2)

    def test_job_lifecycle(self):
        # Keep the test fast: the simulation sleeps between stages otherwise.
        original = demo_server.time.sleep
        demo_server.time.sleep = lambda _seconds: None
        try:
            res = self.client.post("/api/jobs", json={"kind": "factor_evaluate", "params": {}})
            self.assertIn(res.status_code, (200, 202))
            job = res.get_json()
            deadline = time.time() + 5
            while time.time() < deadline:
                job = self.get_json(f"/api/runs/{job['id']}")
                if job["status"] == "completed":
                    break
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["progress"], 100)
            self.assertTrue(job["tail"])
        finally:
            demo_server.time.sleep = original

    def test_unknown_job_is_404(self):
        res = self.client.get("/api/runs/nope")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
