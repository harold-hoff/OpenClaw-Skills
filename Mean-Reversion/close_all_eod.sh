#!/usr/bin/env bash
# End-of-day flatten: market-sell every open Alpaca position.
# Runs from cron at 15:50 America/New_York Mon-Fri.
# Idempotent: prints "no positions" when nothing to do.

set -uo pipefail

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
ALPACA="$WORKSPACE/skills/ALPACA-TRADING/scripts/alpaca_cli.py"
LOG="$WORKSPACE/skills/Mean-Reversion/eod_close.log"
STATE="$WORKSPACE/skills/Mean-Reversion/trading_loop.state.json"

export ALPACA_API_KEY="${ALPACA_API_KEY:-PKO2XKBDPNQ6FR5OKZKMABAHYP}"
export ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-DNpzYNuNWD447GJ6TnVua1D8jM86X2rpnPXYjMkp5uWB}"
export ALPACA_PAPER="${ALPACA_PAPER:-true}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

log "=== EOD close-all run ==="

# 1. Cancel any open orders so they don't get cancelled mid-flatten by Alpaca.
python3 "$ALPACA" cancel all 2>&1 | tee -a "$LOG" || true

# 2. Pull positions JSON; sell each long position with --force.
POS_JSON=$(python3 "$ALPACA" positions --json 2>/dev/null || echo '[]')

python3 - "$POS_JSON" "$ALPACA" "$LOG" <<'PY'
import json, subprocess, sys
positions = json.loads(sys.argv[1] or "[]")
alpaca, log_path = sys.argv[2], sys.argv[3]
if not positions:
    print("[eod] no open positions.")
    sys.exit(0)
for p in positions:
    sym = p.get("symbol")
    qty = int(float(p.get("qty", 0)))
    if not sym or qty == 0:
        continue
    side = "sell" if qty > 0 else "buy"   # cover shorts too
    print(f"[eod] {side.upper()} {abs(qty)} {sym}")
    subprocess.run(["python3", alpaca, "order", side, sym, str(abs(qty)), "--force"])
PY

# 3. Wipe local state so tomorrow starts clean.
echo '{"positions":{}}' > "$STATE"

log "=== EOD close-all complete ==="
