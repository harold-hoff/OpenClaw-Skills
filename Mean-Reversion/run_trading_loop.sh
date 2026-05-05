#!/usr/bin/env bash
# Direct runner for mean-reversion trading loop — no agent overhead.
# Called from cron via systemEvent → exec, or directly from shell.
set -uo pipefail

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
export ALPACA_API_KEY="${ALPACA_API_KEY:-PKO2XKBDPNQ6FR5OKZKMABAHYP}"
export ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-DNpzYNuNWD447GJ6TnVua1D8jM86X2rpnPXYjMkp5uWB}"
export ALPACA_PAPER="${ALPACA_PAPER:-true}"

exec python3 "$WORKSPACE/skills/Mean-Reversion/trading_loop.py" 2>&1
