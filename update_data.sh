#!/usr/bin/env bash
# Safe, incremental data update used by the UI.  This intentionally does not
# run inference or generate a trading plan; prediction remains a separate job.
set -euo pipefail

T="${1:-}"
MODE="${2:-update}"
AUTO=0
if [[ "$T" == "--auto" || -z "$T" ]]; then
  AUTO=1
elif [[ ! "$T" =~ ^[0-9]{8}$ ]]; then
  echo "ERROR: usage update_data.sh [YYYYMMDD|--auto]" >&2
  exit 64
fi
if [[ "$MODE" != "update" && "$MODE" != "--validate-only" ]]; then
  echo "ERROR: second argument must be --validate-only or omitted" >&2
  exit 64
fi

KRONOS="${STOCKLAB_PROJECT_ROOT:-${KRONOS_ROOT:-/opt/kronos}}"
SL="$KRONOS/factor_horizon/shadow_live"
PY="${QUANTWEAVE_PYTHON:-$SL/python}"
[[ -x "$PY" ]] || PY="python3"
[[ -x "$PY" ]] || { echo "ERROR: Python runtime not found" >&2; exit 127; }

RUNTIME_ENV="$KRONOS/finetune_lora/stagewise_rankic_v3/10日ema复用/tail_runtime.env"
[[ -f "$RUNTIME_ENV" ]] || { echo "ERROR: runtime env missing: $RUNTIME_ENV" >&2; exit 2; }
# shellcheck disable=SC1090
source "$RUNTIME_ENV"
export PYTHONPATH="${KRONOS_RUNTIME_PYTHONPATH:-${PYTHONPATH:-}}"
export TOKENIZERS_PARALLELISM=false
TOKEN_SOURCE="${TUSHARE_LEGACY_TOKEN_SOURCE:-}"
[[ -n "$TOKEN_SOURCE" ]] || { echo "ERROR: TUSHARE_LEGACY_TOKEN_SOURCE 未配置（不会伪造更新成功）" >&2; exit 5; }

# With no date the UI asks for a plan rather than guessing a calendar date.
# The planner checks the trading session and updates completed intraday bars
# during the session; after the close it also attempts the official daily
# panel.  An explicit YYYYMMDD keeps the original deterministic behavior.
if [[ "$AUTO" -eq 1 ]]; then
  if [[ "$MODE" == "--validate-only" ]]; then
    exec "$PY" "$(dirname "$0")/update_latest.py" --project-root "$KRONOS" --python "$PY" --token-source "$TOKEN_SOURCE" --plan-only
  fi
  exec "$PY" "$(dirname "$0")/update_latest.py" --project-root "$KRONOS" --python "$PY" --token-source "$TOKEN_SOURCE"
fi

mkdir -p "$SL/data/panels" "$SL/data/cache" "$SL/logs"
PANEL="$SL/data/panels/astock_daily_ohlcva_${T}.parquet"
PREV=$(ls "$SL/data/panels"/astock_daily_ohlcva_*.parquet 2>/dev/null | sort | tail -1 || true)
[[ -n "$PREV" ]] || PREV="$KRONOS/outputs/20260902_official_eval/data/astock_daily_ohlcva_20260902.parquet"
FETCH="$KRONOS/finetune_lora/stagewise_rankic_v3/10日ema复用/fetch_official_close.py"
INDEX="$SL/fetch_index_tail.py"

echo "[stage 1/2] 更新日线面板 ${T}"
if [[ "$MODE" == "--validate-only" ]]; then
  [[ -f "$FETCH" ]] || { echo "ERROR: daily fetch script missing: $FETCH" >&2; exit 3; }
  [[ -f "$INDEX" ]] || { echo "ERROR: index fetch script missing: $INDEX" >&2; exit 4; }
  echo "VALIDATE_OK runtime=$PY panel_dir=$SL/data/panels"
  exit 0
fi
if [[ -f "$PANEL" ]]; then
  echo "panel_hit=$PANEL"
else
  [[ -f "$FETCH" ]] || { echo "ERROR: daily fetch script missing: $FETCH" >&2; exit 3; }
  # Tushare's trade calendar can mark the current day open before its daily
  # rows are published (for example, shortly after the close).  Keep the
  # last complete panel in that case: an unavailable close is not a zero row
  # and must never overwrite the panel with fabricated data.
  FETCH_LOG="$(mktemp "${TMPDIR:-/tmp}/stocklab-fetch.XXXXXX")"
  set +e
  "$PY" "$FETCH" \
    --base-daily "$PREV" --signal-date "$T" --output-daily "$PANEL" \
    --cache-dir "$SL/data/cache" --status-path "$SL/data/fetch_status.json" \
    --legacy-token-source "$TOKEN_SOURCE" >"$FETCH_LOG" 2>&1
  FETCH_RC=$?
  set -e
  cat "$FETCH_LOG"
  if [[ "$FETCH_RC" -ne 0 ]]; then
    # Use POSIX grep here because the service PATH may not include ripgrep.
    if grep -Eq "No daily rows returned for the requested open dates|Official daily close is not ready" "$FETCH_LOG"; then
      PREV_DATE="$(basename "$PREV" | grep -Eo '[0-9]{8}' | tail -1 || true)"
      [[ -n "$PREV_DATE" ]] || PREV_DATE="unknown"
      # Empty/partial responses are cached by the upstream fetch helper.
      # Remove only this date's raw caches so a later retry can see newly
      # published rows; keep the prior complete panel untouched.
      rm -f "$SL/data/cache/daily/$T.parquet" "$SL/data/cache/daily_basic/$T.parquet"
      # Write a machine-readable state consumed by the UI and schedulers.
      "$PY" - "$SL/data/fetch_status.json" "$T" "$PREV" "$PREV_DATE" <<'PY'
import json, os, sys
from datetime import datetime
status_path, target, previous_panel, previous_date = sys.argv[1:]
try:
    lag = (datetime.strptime(target, "%Y%m%d") - datetime.strptime(previous_date, "%Y%m%d")).days
except ValueError:
    lag = None
payload = {
    "status": "deferred",
    "contract": "official-close-ohlcva-v1",
    "signal_date": target,
    "reason": "official daily rows are not published for the open date; previous panel retained",
    "freshness_lag": lag,
    "previous_panel": previous_panel,
    "latest_available_date": previous_date,
    "source": "Tushare daily",
}
os.makedirs(os.path.dirname(status_path), exist_ok=True)
tmp = status_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
os.replace(tmp, status_path)
print(json.dumps(payload, ensure_ascii=False))
PY
      echo "DATA_UPDATE_DEFERRED signal_date=$T latest_panel_date=$PREV_DATE FRESHNESS_LAG=${PREV_DATE}->${T}"
      rm -f "$FETCH_LOG"
      exit 0
    fi
    rm -f "$FETCH_LOG"
    exit "$FETCH_RC"
  fi
  rm -f "$FETCH_LOG"
fi

echo "[stage 2/2] 更新沪深 300 指数尾部 ${T}"
[[ -f "$INDEX" ]] || { echo "ERROR: index fetch script missing: $INDEX" >&2; exit 4; }
"$PY" "$INDEX" --output "$SL/data/index_000300_SH.parquet" \
  --start-date 20260820 --end-date "$T" \
  --legacy-token-source "$TOKEN_SOURCE"

echo "DATA_UPDATE_OK panel=$PANEL index=$SL/data/index_000300_SH.parquet"
