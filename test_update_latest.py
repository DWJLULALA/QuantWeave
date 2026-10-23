"""Planner regression checks; no requests, SSH or market downloads are made."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("update_latest", HERE / "update_latest.py")
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def day(value):
    return dt.datetime.strptime(value, "%Y%m%d").date()


def september_calendar():
    return {dt.date(2026, 9, n): dt.date(2026, 9, n).weekday() < 5 for n in range(1, 31)}


class DailyPlanTests(unittest.TestCase):
    def test_preopen_and_intraday_catch_up_every_missing_close(self):
        for clock in ("09:20", "09:31", "11:00", "14:30"):
            with self.subTest(clock=clock):
                plan = planner._daily_plan(dt.datetime.fromisoformat("2026-09-17T" + clock),
                                           september_calendar(), day("20260914"))
                self.assertEqual(plan["target"], "20260916")
                self.assertEqual(plan["pending_dates"], ["20260915", "20260916"])

    def test_after_close_includes_today_after_old_missing_sessions(self):
        plan = planner._daily_plan(dt.datetime(2026, 9, 17, 15, 5),
                                   september_calendar(), day("20260914"))
        self.assertEqual(plan["pending_dates"], ["20260915", "20260916", "20260917"])

    def test_weekend_and_explicit_holiday_still_catch_up_history(self):
        sessions = september_calendar()
        sessions[day("20260921")] = False
        for now in (dt.datetime(2026, 9, 19, 10), dt.datetime(2026, 9, 21, 16)):
            with self.subTest(now=now):
                plan = planner._daily_plan(now, sessions, day("20260916"))
                self.assertEqual(plan["target"], "20260918")
                self.assertEqual(plan["pending_dates"], ["20260917", "20260918"])

    def test_missing_calendar_never_skips_over_an_unknown_weekday(self):
        sessions = september_calendar()
        del sessions[day("20260916")]
        plan = planner._daily_plan(dt.datetime(2026, 9, 17, 16), sessions, day("20260914"))
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["pending_dates"], ["20260915"])
        self.assertEqual(plan["missing_calendar_dates"], ["20260916"])
        self.assertIsNone(planner._trading_day(day("20260916"), sessions))

    def test_no_missing_sessions_is_up_to_date(self):
        plan = planner._daily_plan(dt.datetime(2026, 9, 17, 9, 20),
                                   september_calendar(), day("20260916"))
        self.assertEqual(plan["status"], "up_to_date")
        self.assertEqual(plan["pending_dates"], [])

    def test_no_baseline_is_blocked(self):
        plan = planner._daily_plan(dt.datetime(2026, 9, 17, 9, 20), september_calendar(), None)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["pending_dates"], [])

    def test_today_deferred_does_not_prevent_older_dates_being_saved(self):
        plan = planner._daily_plan(dt.datetime(2026, 9, 17, 16),
                                   september_calendar(), day("20260914"))
        results = [(0, "DATA_UPDATE_OK"), (0, "DATA_UPDATE_OK"), (0, "DATA_UPDATE_DEFERRED")]
        with mock.patch.object(planner, "_run", side_effect=results) as run, contextlib.redirect_stdout(io.StringIO()):
            status = planner._refresh_daily(plan, HERE, False)
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["20260915", "20260916", "20260917"])
        self.assertEqual(status["completed_dates"], ["20260915", "20260916"])
        self.assertEqual(status["latest_panel_date"], "20260916")
        self.assertEqual(status["blocked_at"], "20260917")
        self.assertEqual(status["status"], "deferred")

    def test_missing_older_close_stops_before_newer_dates(self):
        plan = planner._daily_plan(dt.datetime(2026, 9, 17, 16),
                                   september_calendar(), day("20260914"))
        for result, expected in (((0, "DATA_UPDATE_DEFERRED"), "deferred"), ((1, "network failed"), "failed")):
            with self.subTest(result=result), mock.patch.object(planner, "_run", return_value=result) as run, contextlib.redirect_stdout(io.StringIO()):
                status = planner._refresh_daily(plan, HERE, False)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(status["status"], expected)
                self.assertEqual(status["latest_panel_date"], "20260914")


class CalendarTests(unittest.TestCase):
    def test_integer_and_float_dates_do_not_parse_as_1970(self):
        for values in ([20260916, 20260917], [20260916.0, 20260917.0],
                       ["20260916", "20260917"], ["2026-09-16", "2026-09-17"]):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                pd.DataFrame({"cal_date": values, "is_open": [1, 0]}).to_parquet(Path(directory) / "SSE_20260101_20261231.parquet")
                sessions = planner._load_calendar(Path(directory))
                self.assertTrue(sessions[day("20260916")])
                self.assertFalse(sessions[day("20260917")])
                self.assertIsNone(planner._trading_day(day("20260918"), sessions))

    def test_missing_or_invalid_calendar_is_not_assumed_to_be_weekdays(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(planner._is_trading_day(day("20260917"), Path(directory)))
            self.assertFalse(planner._is_trading_day(day("20260919"), Path(directory)))
            pd.DataFrame({"cal_date": [20260917]}).to_parquet(Path(directory) / "SSE_20260917_20260917.parquet")
            self.assertIsNone(planner._is_trading_day(day("20260917"), Path(directory)))


class PlanOnlyTests(unittest.TestCase):
    def make_project(self, root):
        data = root / "factor_horizon/shadow_live/data"
        (data / "cache/trade_cal").mkdir(parents=True)
        sessions = september_calendar()
        pd.DataFrame({"cal_date": [int(d.strftime("%Y%m%d")) for d in sessions],
                      "is_open": [int(v) for v in sessions.values()]}).to_parquet(data / "cache/trade_cal/SSE_20260901_20260930.parquet")
        (data / "panels").mkdir()
        (data / "panels/astock_daily_ohlcva_20260914.parquet").touch()
        minute_data = root / "finetune_lora/data"
        minute_data.mkdir(parents=True)
        (minute_data / "astock_30min_ohlcva_full_20260916.parquet").touch()
        return data

    def test_validation_keeps_real_status_and_never_runs_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self.make_project(root)
            real_status = data / "update_status.json"
            original = '{"status":"completed","intraday":{"cutoff":"2026-09-16 15:00:00"}}'
            real_status.write_text(original)
            before = real_status.stat().st_mtime_ns
            argv = ["update_latest.py", "--project-root", str(root), "--now", "2026-09-17T11:00:00", "--plan-only"]
            output = io.StringIO()
            with mock.patch("sys.argv", argv), mock.patch.object(planner, "_run") as run, contextlib.redirect_stdout(output):
                self.assertEqual(planner.main(), 0)
            run.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "planned")
            self.assertEqual(result["daily"]["status"], "planned")
            self.assertEqual(result["intraday"]["status"], "planned")
            self.assertEqual(real_status.read_text(), original)
            self.assertEqual(real_status.stat().st_mtime_ns, before)

    def test_empty_project_validation_does_not_create_status_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = ["update_latest.py", "--project-root", directory, "--now", "2026-09-17T09:20:00", "--plan-only"]
            with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(planner.main(), 0)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_timezone_and_intraday_then_daily_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            argv = ["update_latest.py", "--project-root", str(root), "--now", "2026-09-17T03:00:00+00:00"]
            with mock.patch("sys.argv", argv), mock.patch.object(planner, "_run", return_value=(0, "DATA_UPDATE_OK")) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(planner.main(), 0)
            self.assertIn("update_intraday_market_parallel.py", run.call_args_list[0].args[0][1])
            self.assertEqual([c.args[0][-1] for c in run.call_args_list[1:]], ["20260915", "20260916"])
            status = json.loads((root / "factor_horizon/shadow_live/data/update_status.json").read_text())
            self.assertEqual(status["intraday"]["cutoff"], "2026-09-17 11:00:00")
            self.assertEqual(status["daily"]["latest_panel_date"], "20260916")


if __name__ == "__main__":
    unittest.main()
