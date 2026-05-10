#!/usr/bin/env bash
# End-of-day flatten: market-sell every open Alpaca position.
# Runs from cron at 15:50 / 15:55 / 15:58 America/New_York Mon-Fri.
# Idempotent: writes a dated flag file so multiple runs are safe.

set -uo pipefail

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
ALPACA="$WORKSPACE/skills/ALPACA-TRADING/scripts/alpaca_cli.py"
LOG="$WORKSPACE/skills/Mean-Reversion/eod_close.log"
STATE="$WORKSPACE/skills/Mean-Reversion/trading_loop.state.json"
FLAG="$WORKSPACE/skills/Mean-Reversion/eod.flag"

export ALPACA_API_KEY="${ALPACA_API_KEY:-}"
export ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-}"
export ALPACA_PAPER="${ALPACA_PAPER:-true}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

if [ -z "$ALPACA_API_KEY" ] || [ -z "$ALPACA_SECRET_KEY" ]; then
    log "ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set — cannot run EOD flatten."
    exit 1
fi

# Idempotency: skip if today's flag already exists.
TODAY="$(TZ=America/New_York date +%Y-%m-%d)"
if [ -f "$FLAG" ] && [ "$(cat "$FLAG" 2>/dev/null | tr -d '[:space:]')" = "$TODAY" ]; then
    log "EOD already ran today ($TODAY) — exit 0."
    exit 0
fi

log "=== EOD close-all run ($TODAY) ==="

python3 "$ALPACA" cancel all 2>&1 | tee -a "$LOG" || true

POS_JSON=$(python3 "$ALPACA" positions --json 2>/dev/null || echo '{}')

python3 - "$POS_JSON" "$ALPACA" "$LOG" <<'PY'
import json, subprocess, sys
raw = json.loads(sys.argv[1] or "{}")
# alpaca_cli returns {"positions": [...]}; tolerate older bare-list output too.
if isinstance(raw, dict):
    positions = raw.get("positions", [])
elif isinstance(raw, list):
    positions = raw
else:
    positions = []
alpaca, log_path = sys.argv[2], sys.argv[3]
if not positions:
    print("[eod] no open positions.")
    sys.exit(0)
print(f"[eod] flattening {len(positions)} positions")
for p in positions:
    if not isinstance(p, dict):
        continue
    sym = p.get("symbol")
    qty = int(float(p.get("qty", 0)))
    if not sym or qty == 0:
        continue
    side = "sell" if qty > 0 else "buy"
    print(f"[eod] {side.upper()} {abs(qty)} {sym}")
    subprocess.run(
        ["python3", alpaca, "order", side, sym, str(abs(qty)), "--force"],
        timeout=30,
    )
PY

echo '{"positions":{}}' > "$STATE"
echo -n "$TODAY" > "$FLAG"

log "=== EOD close-all complete ==="
