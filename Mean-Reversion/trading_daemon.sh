#!/usr/bin/env bash
# Sequential trading daemon — runs OUTSIDE OpenClaw gateway/cron (no LLM).
# Mean-Reversion scan → Sentiment cycle → idempotent EOD flatten hook → sleep.
#
# Env:
#   TRADER_SLEEP_OPEN_SEC    pause between cycles when Alpaca says market open (default 75)
#   TRADER_SLEEP_CLOSED_SEC  when closed overnight/weekend (default 300)
#   SKIP_SENTIMENT_CYCLE     set to 1 to only run mean-reversion + EOD (lighter load)

set +e

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
MR_LOOP="$WORKSPACE/skills/Mean-Reversion/run_trading_loop.sh"
SENTIMENT="$WORKSPACE/skills/Sentiment-Trader/run_cycle.sh"
EOD="$WORKSPACE/skills/Mean-Reversion/close_all_eod.sh"
LOG="$WORKSPACE/skills/Mean-Reversion/trading_daemon.log"

SLEEP_OPEN="${TRADER_SLEEP_OPEN_SEC:-75}"
SLEEP_CLOSED="${TRADER_SLEEP_CLOSED_SEC:-300}"

log() {
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"
}

alpaca_market_open() {
  python3 - <<'PY'
import os, sys
try:
    from alpaca.trading.client import TradingClient
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not k or not s:
        sys.exit(1)
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    c = TradingClient(k, s, paper=paper)
    sys.exit(0 if c.get_clock().is_open else 1)
except Exception:
    sys.exit(1)
PY
}

log "=== trading_daemon starting (sequential MR->sentiment->EOD check); OPEN_SLEEP=${SLEEP_OPEN}s CLOSED_SLEEP=${SLEEP_CLOSED}s ==="

while true; do
  alpaca_market_open
  OPEN=$?

  if [ "$OPEN" -eq 0 ]; then
    log "--- cycle: mean-reversion ---"
    bash "$MR_LOOP" >>"$LOG" 2>&1
    EC_MR=$?
    [ "$EC_MR" -ne 0 ] && log "mean-reversion exit code=$EC_MR"
    if [ "${SKIP_SENTIMENT_CYCLE:-0}" != "1" ]; then
      log "--- cycle: sentiment ---"
      bash "$SENTIMENT" >>"$LOG" 2>&1
      EC_SE=$?
      [ "$EC_SE" -ne 0 ] && log "sentiment exit code=$EC_SE"
    fi
    NEXT_SLEEP="$SLEEP_OPEN"
  else
    log "--- Alpaca clock: market CLOSED (sleep ${SLEEP_CLOSED}s; skip MR/sentiment) ---"
    NEXT_SLEEP="$SLEEP_CLOSED"
  fi

  # Idempotent per ET calendar day (script internals skip after successful flatten).
  if command -v timeout >/dev/null 2>&1; then
    timeout 300 bash "$EOD" >>"$LOG" 2>&1 || log "EOD hook: timeout or exit $? (continuing loop)"
  else
    bash "$EOD" >>"$LOG" 2>&1 || true
  fi

  sleep "$NEXT_SLEEP"
done
