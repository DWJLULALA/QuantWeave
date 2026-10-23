#!/bin/bash
# 交易日自动推演流水线（QuantWeave）。
# 用法: daily_pipeline.sh morning|evening
#   morning: 盘中 10:05 — 分时数据更新 → 分时模型推演 → 分时口径因子评估
#   evening: 盘后 17:30 — 官方日线更新(未发布则重试) → 日线模型推演 → 日线口径因子评估
# 全部通过本机 127.0.0.1:7070 面板 API 提交，不在脚本里拼任何 shell 命令。
set -uo pipefail
MODE="${1:-evening}"
API="${QUANTWEAVE_API:-http://127.0.0.1:7070}"
UI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TODAY="$(date +%F)"
log(){ echo "[$(date '+%F %T')] $*"; }

post_job(){ # $1 kind  $2 params-json
  curl -s --noproxy '*' -X POST "$API/api/jobs" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"$1\",\"params\":$2}"
}
wait_job(){ # $1 id  $2 timeout-sec
  local id="$1" timeout="${2:-3600}" waited=0 S
  while :; do
    S=$(curl -s --noproxy '*' "$API/api/runs" | python3 -c "
import json,sys
try:
    j=[x for x in json.load(sys.stdin) if x['id']=='$id'][0]
    print(j['status'])
except Exception:
    print('unknown')")
    case "$S" in
      completed) echo ok; return 0;;
      failed|error) echo failed; return 1;;
    esac
    sleep 20; waited=$((waited+20))
    if [ "$waited" -ge "$timeout" ]; then echo timeout; return 2; fi
  done
}
run_step(){ # $1 名称  $2 kind  $3 params  $4 timeout
  local out id st
  out=$(post_job "$2" "$3")
  id=$(echo "$out" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('id',''))
except Exception: print('')")
  if [ -z "$id" ]; then log "$1 提交失败: $(echo "$out" | head -c 200)"; return 1; fi
  st=$(wait_job "$id" "$4")
  log "$1 -> $st (job=$id)"
  [ "$st" = ok ]
}

# --- 日历自愈（守卫前）：缺口会让 validate-only 永远返回 trading_day=none ---
RUNTIME_ENV="${KRONOS_ROOT:-/opt/kronos}/finetune_lora/stagewise_rankic_v3/10日ema复用/tail_runtime.env"
if [[ -f "$RUNTIME_ENV" ]]; then source "$RUNTIME_ENV"; fi
if [[ -n "${TUSHARE_LEGACY_TOKEN_SOURCE:-}" ]]; then
  python3 "$UI/calendar_heal.py" \
    --end-date "$(date +%Y%m%d)" --legacy-token-source "$TUSHARE_LEGACY_TOKEN_SOURCE" \
    >> "$UI/logs/calendar_heal.log" 2>&1 || log "日历自愈失败（继续用现有日历）"
fi

# --- 非交易日守卫：validate-only 计划器判断 ---
VD=$(cd "$UI" && bash update_data.sh --auto --validate-only 2>/dev/null | tail -1)
TRADING=$(echo "$VD" | python3 -c "
import json,sys
try: print(str(json.load(sys.stdin).get('trading_day')).lower())
except Exception: print('unknown')")
if [ "$TRADING" != "true" ]; then
  log "今天 $TODAY 非交易日（trading_day=$TRADING），流水线跳过"
  exit 0
fi
log "=== $MODE 流水线开始（$TODAY）==="

if [ "$MODE" = "morning" ]; then
  run_step "分时数据更新" data_update '{}' 3600
  run_step "大盘与行业趋势刷新" trend_refresh '{}' 900 || log "趋势刷新未完成，保留上次快照"
  ok=1
  for a in 1 2; do
    run_step "分时模型推演(第${a}次)" intraday_predict '{}' 7200 && { ok=0; break; }
    log "分时推演未完成，15 分钟后重试（GPU 可能被占用）"
    sleep 900
  done
  [ "$ok" = 0 ] && run_step "分时因子评估" factor_evaluate "{\"date\":\"$TODAY\",\"kronos_source\":\"intraday\"}" 3600 \
    || log "分时推演两次均未成功，跳过分时因子评估"
elif [ "$MODE" = "evening" ]; then
  # 官方日线可能滞后发布：deferred 时每 30 分钟重试，最多 3 轮
  daily_ok=0
  for a in 1 2 3; do
    run_step "数据更新(第${a}轮)" data_update '{}' 3600
    LATEST=$(curl -s --noproxy '*' "$API/api/status" | python3 -c "
import json,sys
try: print(json.load(sys.stdin)['data'].get('latest_panel_date',''))
except Exception: print('')")
    if [ "$LATEST" = "$(echo "$TODAY" | tr -d -)" ]; then daily_ok=1; log "日线已更新到 $LATEST"; break; fi
    log "日线尚未发布（面板=$LATEST），30 分钟后重试"
    [ "$a" -lt 3 ] && sleep 1800
  done
  if [ "$daily_ok" = 1 ]; then
    run_step "大盘与行业趋势刷新" trend_refresh '{}' 900 || log "趋势刷新未完成，保留上次快照"
    ok=1
    for a in 1 2 3; do
      run_step "日线模型推演(第${a}次)" predict "{\"date\":\"$TODAY\"}" 7200 && { ok=0; break; }
      log "日线推演未完成，20 分钟后重试（GPU 可能被占用）"
      sleep 1200
    done
    [ "$ok" = 0 ] && run_step "纯四期限因子评估" factor_evaluate "{\"date\":\"$TODAY\",\"kronos_source\":\"composite\",\"top_k\":100,\"exit_rank\":1000}" 3600 \
      || log "日线推演三次均未成功，跳过因子评估"
    log "大盘浪型因子刷新"
    log "缠论阶段因子刷新"
    python3 "$UI/chan_phase_engine.py" >/dev/null 2>&1 \
      && log "缠论阶段因子 -> ok" || log "缠论阶段因子 -> failed"
    python3 "$UI/market_wave_factor.py" --refresh >/dev/null 2>&1 \
      && log "大盘浪型因子 -> ok" || log "大盘浪型因子 -> failed"
  else
    log "官方日线三轮均未发布（提供方滞后），今晚跳过日模型；明早分时链照常"
  fi
else
  log "未知模式: $MODE（用 morning 或 evening）"; exit 64
fi
log "=== $MODE 流水线结束 ==="
