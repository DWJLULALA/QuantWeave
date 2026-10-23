#!/usr/bin/env python3
"""Refresh the 30-minute panel, run Kronos, then build timing reminders.

The refresh is incremental: the Sina updater appends newest bars to a copy of
the most recent panel. The original dated panel is never replaced.
"""
from __future__ import annotations
import argparse, datetime as dt, json, re, subprocess, sys
from pathlib import Path
DATE_RE = re.compile(r"(?:full|panel)[_\-](\d{8})")
def _panel_candidates(project_root: Path) -> list[Path]:
    data = project_root / "finetune_lora" / "data"
    # Provider experiments are deliberately isolated: Tushare's historical
    # mirror is EOD-only and EastMoney panels may be partial. The live workflow
    # must consume the canonical Sina/market panel unless a caller explicitly
    # runs a provider-specific updater and passes its output to the engine.
    return sorted(
        [p for p in data.glob("astock_30min_ohlcva_full_*.parquet")
         if "_em_" not in p.stem and "_tushare_" not in p.stem],
        key=lambda p: p.stat().st_mtime,
    )
def _panel_date(path: Path):
    m=DATE_RE.search(path.name)
    try: return dt.datetime.strptime(m.group(1), "%Y%m%d").date() if m else None
    except ValueError: return None
def _latest_calendar_date(calendar_dir: Path, today: dt.date) -> dt.date:
    dates=[]
    for f in calendar_dir.glob("SSE_*_*.parquet") if calendar_dir.exists() else []:
        m=re.search(r"SSE_(\d{8})_\d{8}\.parquet$", f.name)
        if m:
            try:
                d=dt.datetime.strptime(m.group(1), "%Y%m%d").date()
                if d <= today: dates.append(d)
            except ValueError: pass
    if dates and max(dates) >= today: return max(dates)
    # The calendar cache is written by the daily pipeline (i.e. after market close),
    # so on a NEW trading day it always lags behind today — trusting it here made
    # the intraday refresh silently no-op until the daily run happened.  Treat the
    # last weekday as a candidate session day so refresh fires; if today is really
    # a holiday the probes fail cheaply and we fall back to reusing the panel.
    # [infra 20260918]
    while today.weekday() >= 5: today -= dt.timedelta(days=1)
    return today
def _desired_completed_bar(session_day: dt.date, now: dt.datetime) -> dt.datetime | None:
    if now.date() != session_day or now.weekday() >= 5: return None
    hm = now.hour * 60 + now.minute
    ends = [(10,0),(10,30),(11,0),(11,30),(13,30),(14,0),(14,30),(15,0)]
    bars = [dt.datetime.combine(session_day, dt.time(h,m)) for h,m in ends if hm >= h*60+m]
    return bars[-1] if bars else None
def _panel_max_timestamp(panel: Path) -> dt.datetime | None:
    try:
        summary = panel.with_suffix('.summary.json')
        value = json.loads(summary.read_text(encoding='utf-8')).get('max_timestamp') if summary.exists() else None
        return dt.datetime.fromisoformat(str(value)) if value else None
    except Exception: return None
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--project-root',default='/opt/kronos'); ap.add_argument('--output-dir',required=True); ap.add_argument('--device',default='auto'); ap.add_argument('--gpu-wait-seconds',type=int,default=900); ap.add_argument('--top-k',type=int,default=5); ap.add_argument('--batch-size',type=int,default=1024); ap.add_argument('--max-gpus',type=int,default=4); ap.add_argument('--workers-per-gpu',type=int,default=2); ap.add_argument('--refresh',choices=('auto','always','never'),default='auto'); ap.add_argument('--refresh-limit-codes',type=int,default=0); ap.add_argument('--refresh-workers',type=int,default=8); ap.add_argument('--dry-refresh',action='store_true'); ap.add_argument('--calendar-dir',default=None); ap.add_argument('--state-dir',default=None); a=ap.parse_args()
    project=Path(a.project_root); out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True); data_dir=project/'finetune_lora'/'data'; calendar_dir=Path(a.calendar_dir or project/'factor_horizon'/'shadow_live'/'data'/'cache'/'trade_cal'); candidates=_panel_candidates(project)
    if not candidates: raise FileNotFoundError(f'no 30-minute panel under {data_dir}')
    source=candidates[-1]; now=dt.datetime.now(); today=now.date(); session_day=_latest_calendar_date(calendar_dir,today); source_day=_panel_date(source); desired=_desired_completed_bar(session_day,now); panel_max=_panel_max_timestamp(source); intraday_stale=bool(desired and (panel_max is None or panel_max < desired)); should_refresh=a.refresh=='always' or (a.refresh=='auto' and (source_day is None or source_day<session_day or intraday_stale)); updater=project/'finetune_lora'/'update_intraday_sina_parallel.py'; refresh_backend=None; refresh_skip=''; panel=source
    local_updater=Path(__file__).with_name('update_intraday_market_parallel.py')
    if local_updater.exists(): updater=local_updater
    rt_updater=Path(__file__).with_name('update_intraday_rt.py')
    if a.dry_refresh:
        print(json.dumps({'available':True,'mode':'dry_refresh','source_panel':str(source),'source_day':source_day.isoformat() if source_day else None,'session_day':session_day.isoformat(),'refresh':should_refresh,'desired_completed_bar':desired.isoformat() if desired else None,'strategy':'probe rt_min(1 call) -> probe eastmoney(3 codes, no retry) -> skip refresh and reuse existing panel; never grind a dead source for the full market'},ensure_ascii=False,indent=2)); return
    if should_refresh:
        asof=today.strftime('%Y%m%d'); cutoff_args=['--cutoff',desired.strftime('%Y-%m-%d %H:%M:%S')] if desired else []
        if rt_updater.exists():
            pr=subprocess.run([sys.executable,str(rt_updater),'--project-root',str(project),'--probe','--as-of-date',asof],capture_output=True,text=True,timeout=120)
            if pr.returncode==0:
                panel=data_dir/f'astock_30min_ohlcva_full_{today:%Y%m%d}_ui_parallel.parquet'
                full=[sys.executable,str(rt_updater),'--project-root',str(project),'--input-panel',str(source),'--output-panel',str(panel),'--as-of-date',asof]+cutoff_args+['--workers','4']
                if subprocess.run(full).returncode!=0:
                    refresh_skip='rt_min_full_run_failed'; panel=source
                else:
                    refresh_backend='tushare_rt_min'
            else:
                refresh_skip=f'rt_min_probe_rc{pr.returncode}'
        if refresh_backend is None:
            probe_out=data_dir/f'_em_probe_{asof}.parquet'
            pc=[sys.executable,str(updater),'--input-panel',str(source),'--output-panel',str(probe_out),'--as-of-date',asof,'--scale','30','--datalen','240','--workers','4','--retries','0','--limit-codes','3']+cutoff_args
            try: subprocess.run(pc,capture_output=True,text=True,timeout=300)
            except subprocess.TimeoutExpired: pass
            probe_ok=False
            try:
                s=json.loads(probe_out.with_suffix('.summary.json').read_text(encoding='utf-8'))
                probe_ok=int(s.get('failure_count') or 0)<3 and int(s.get('new_rows_fetched') or 0)>0
            except Exception: probe_ok=False
            for f in (probe_out,probe_out.with_suffix('.summary.json')):
                try: f.unlink()
                except FileNotFoundError: pass
            if probe_ok:
                panel=data_dir/f'astock_30min_ohlcva_full_{today:%Y%m%d}_ui_parallel.parquet'
                cmd=[sys.executable,str(updater),'--input-panel',str(source),'--output-panel',str(panel),'--as-of-date',asof,'--scale','30','--datalen','240','--workers',str(a.refresh_workers),'--retries','4']+cutoff_args
                if a.refresh_limit_codes>0: cmd+=['--limit-codes',str(a.refresh_limit_codes)]
                subprocess.run(cmd,check=True); refresh_backend='eastmoney'
            else:
                refresh_skip=(refresh_skip+'+' if refresh_skip else '')+'eastmoney_probe_failed'
                # third fallback: Sina rolling 30-min window (no --cutoff support;
                # the updater dedups on (ts_code, timestamps) on append) [infra 20260918]
                sina_updater=project/'finetune_lora'/'update_intraday_sina_parallel.py'
                if sina_updater.exists():
                    s_probe=data_dir/f'_sina_probe_{asof}.parquet'
                    sc=[sys.executable,str(sina_updater),'--input-panel',str(source),'--output-panel',str(s_probe),'--as-of-date',asof,'--scale','30','--datalen','240','--workers','4','--limit-codes','3','--retries','0']
                    try: subprocess.run(sc,capture_output=True,text=True,timeout=300)
                    except subprocess.TimeoutExpired: pass
                    s_ok=False
                    try:
                        s=json.loads(s_probe.with_suffix('.summary.json').read_text(encoding='utf-8'))
                        s_ok=int(s.get('failure_count') or 0)<3 and int(s.get('new_rows_fetched') or 0)>0
                    except Exception: s_ok=False
                    for f in (s_probe,s_probe.with_suffix('.summary.json')):
                        try: f.unlink()
                        except FileNotFoundError: pass
                    if s_ok:
                        panel=data_dir/f'astock_30min_ohlcva_full_{today:%Y%m%d}_ui_parallel.parquet'
                        cmd=[sys.executable,str(sina_updater),'--input-panel',str(source),'--output-panel',str(panel),'--as-of-date',asof,'--scale','30','--datalen','240','--workers',str(max(a.refresh_workers,8)),'--retries','2']
                        if a.refresh_limit_codes>0: cmd+=['--limit-codes',str(a.refresh_limit_codes)]
                        subprocess.run(cmd,check=True); refresh_backend='sina_30min'
                    else:
                        refresh_skip=refresh_skip+'+sina_probe_failed'
            if refresh_backend is None:
                print(json.dumps({'refresh':'skipped','reason':refresh_skip,'reusing_panel':str(panel),'panel_max':panel_max.isoformat() if panel_max else None},ensure_ascii=False),flush=True)
    engine=Path(__file__).with_name('engine_intraday.py'); subprocess.run([sys.executable,str(engine),'--output-dir',str(out),'--panel-path',str(panel),'--device',a.device,'--gpu-wait-seconds',str(a.gpu_wait_seconds),'--batch-size',str(a.batch_size),'--max-gpus',str(a.max_gpus),'--workers-per-gpu',str(a.workers_per_gpu),'--calendar-dir',str(calendar_dir)],check=True)
    pred=out/'intraday_next_session_predictions.csv'; timing=Path(__file__).with_name('intraday_timing.py'); state_dir=Path(a.state_dir) if a.state_dir else (Path(__file__).parent/'data'/'intraday'); cmd=[sys.executable,str(timing),'--project-root',str(project),'--output-dir',str(out),'--top-k',str(a.top_k),'--predictions',str(pred),'--state-dir',str(state_dir)]; positions=Path(__file__).parent/'data'/'intraday_positions.json';
    if positions.exists(): cmd += ['--positions',str(positions)]
    subprocess.run(cmd,check=True)
    reports=sorted(out.glob('intraday_timing_*.json')); report=reports[-1] if reports else None; print(json.dumps({'available':True,'prediction_manifest':str(out/'manifest.json'),'timing':str(report) if report else None,'panel_path':str(panel),'refresh':refresh_backend,'refresh_backend':refresh_backend,'refresh_skip_reason':refresh_skip or None,'session_day':session_day.isoformat(),'desired_completed_bar':desired.isoformat() if desired else None,'source_panel_max':panel_max.isoformat() if panel_max else None,'freshness':'refreshed' if refresh_backend else ('current' if not intraday_stale else 'stale'),'mode':'latest_observed_160_bars_incremental_refresh'},ensure_ascii=False))
if __name__=='__main__': main()
