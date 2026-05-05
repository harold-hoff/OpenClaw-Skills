#!/usr/bin/env bash
# Sentiment-Trader 15-minute cycle runner.
# Fetches news, scores sentiment, and places trades autonomously.
# Called by cron — no agent turn overhead.

set -uo pipefail

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
WATCHLIST="$WORKSPACE/skills/Mean-Reversion/watchlist.default.txt"
NEWS="$WORKSPACE/skills/Sentiment-Trader/scripts/news-fetcher.py"
SCORE="$WORKSPACE/skills/Sentiment-Trader/scripts/sentiment_score.py"
ALPACA="$WORKSPACE/skills/ALPACA-TRADING/scripts/alpaca_cli.py"
LOG="$WORKSPACE/skills/Sentiment-Trader/cycle.log"
STATE_FILE="$WORKSPACE/skills/Sentiment-Trader/state.json"

export ALPACA_API_KEY="${ALPACA_API_KEY:-PKO2XKBDPNQ6FR5OKZKMABAHYP}"
export ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-DNpzYNuNWD447GJ6TnVua1D8jM86X2rpnPXYjMkp5uWB}"
export ALPACA_PAPER="${ALPACA_PAPER:-true}"
export FINNHUB_API_KEY="${FINNHUB_API_KEY:-d7p8ks9r01qlb0a95go0d7p8ks9r01qlb0a95gog}"
export NEWS_API_KEY="${NEWS_API_KEY:-82cba43d72d34c5aa03b6b5d2a7afa08}"
export ALPHA_VANTAGE_API_KEY="${ALPHA_VANTAGE_API_KEY:-8J447O4D6D765H3J}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

NOW_TS=$(date +%s)
log "=== Sentiment cycle start ==="

# --- Account check ---
ACCT_JSON=$(python3 "$ALPACA" account --json 2>/dev/null || echo '{}')
EQUITY=$(echo "$ACCT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('equity',100000))" 2>/dev/null || echo "100000")
BP=$(echo "$ACCT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('buying_power',0))" 2>/dev/null || echo "0")
log "Equity: \$$EQUITY | Buying power: \$$BP"

# --- Load state (start-of-day equity, last trade times) ---
START_EQUITY=$(python3 -c "
import json, os, time
sf = '$STATE_FILE'
today = time.strftime('%Y-%m-%d', time.gmtime())
if os.path.exists(sf):
    d = json.load(open(sf))
    if d.get('date') == today:
        print(d.get('start_equity', $EQUITY))
        exit()
print($EQUITY)
" 2>/dev/null || echo "$EQUITY")

# Save state if new day
python3 -c "
import json, os, time
sf = '$STATE_FILE'
today = time.strftime('%Y-%m-%d', time.gmtime())
d = {}
if os.path.exists(sf):
    d = json.load(open(sf))
if d.get('date') != today:
    d = {'date': today, 'start_equity': $EQUITY, 'trades': {}}
    json.dump(d, open(sf, 'w'))
" 2>/dev/null || true

# --- Drawdown check: >20% → halt (was 5%) ---
DRAWDOWN=$(python3 -c "print(($START_EQUITY - $EQUITY) / $START_EQUITY * 100)" 2>/dev/null || echo "0")
if python3 -c "exit(0 if float('$DRAWDOWN') >= 20 else 1)" 2>/dev/null; then
    log "HARD STOP: drawdown ${DRAWDOWN}% >= 20%. Closing all positions."
    python3 "$ALPACA" positions --json 2>/dev/null | python3 -c "
import sys, json, subprocess
positions = json.load(sys.stdin)
for p in positions:
    sym = p.get('symbol','')
    qty = int(float(p.get('qty',0)))
    if qty > 0:
        subprocess.run(['python3', '$ALPACA', 'order', 'sell', sym, str(qty), '--force'])
" 2>/dev/null || true
    exit 0
fi

# --- Fear & Greed ---
FEAR_GREED=$(python3 -c "
import json, sys
raw = open('/dev/stdin').read() if not sys.stdin.isatty() else '{}'
" 2>/dev/null || echo "50")
# Get from a quick news-fetcher call for one ticker
FG_RAW=$(timeout 15 python3 "$NEWS" AAPL 2>/dev/null || echo '{}')
FEAR_GREED=$(echo "$FG_RAW" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('fear_greed',{}).get('score',50))" 2>/dev/null || echo "50")
log "Fear & Greed: $FEAR_GREED"

# Skip ALL buys only if extreme fear (fear_greed < 10, was 20)
if python3 -c "exit(0 if float('$FEAR_GREED') < 10 else 1)" 2>/dev/null; then
    log "Fear & Greed < 10: skipping all BUY orders this cycle."
    BUY_BLOCKED=1
else
    BUY_BLOCKED=0
fi

# Position size multiplier: 0.5 if fear_greed > 80
SIZE_MULT=$(python3 -c "print(0.5 if float('$FEAR_GREED') > 80 else 1.0)" 2>/dev/null || echo "1.0")

# --- Get current positions ---
POSITIONS_JSON=$(python3 "$ALPACA" positions --json 2>/dev/null || echo '[]')

# --- Per-ticker cycle ---
while IFS= read -r line; do
    # Strip comments and whitespace
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [[ -z "$line" ]] && continue

    # Handle comma-separated
    IFS=',' read -ra TICKERS <<< "$line"
    for SYM in "${TICKERS[@]}"; do
        SYM="$(echo "$SYM" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')"
        [[ -z "$SYM" ]] && continue

        log "-- $SYM"

        # Check last trade time (60-min cooldown)
        LAST_TRADE=$(python3 -c "
import json, os, time
sf = '$STATE_FILE'
if os.path.exists(sf):
    d = json.load(open(sf))
    trades = d.get('trades', {})
    print(trades.get('$SYM', 0))
else:
    print(0)
" 2>/dev/null || echo "0")
        SINCE=$(( NOW_TS - ${LAST_TRADE:-0} ))
        if [ "$SINCE" -lt 900 ]; then
            log "  SKIP: traded ${SYM} ${SINCE}s ago (15-min cooldown)."
            continue
        fi

        # Fetch and score
        RAW=$(timeout 20 python3 "$NEWS" "$SYM" 2>/dev/null || echo '{}')
        RESULT=$(echo "$RAW" | python3 "$SCORE" "$SYM" "$RAW" 2>/dev/null || echo '{"signal":"NEUTRAL","score":0,"article_count":0}')

        SIGNAL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('signal','NEUTRAL'))" 2>/dev/null || echo "NEUTRAL")
        ARTICLE_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('article_count',0))" 2>/dev/null || echo "0")
        SCORE_VAL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('score',0))" 2>/dev/null || echo "0")

        log "  signal=$SIGNAL score=$SCORE_VAL articles=$ARTICLE_COUNT"

        # Guardrail: non-NEUTRAL signal only (article count not required)
        if [ "$SIGNAL" = "NEUTRAL" ]; then
            log "  SKIP: NEUTRAL signal."
            continue
        fi
        if python3 -c "exit(0 if float('$BP') < 100 else 1)" 2>/dev/null; then
            log "  SKIP: buying power \$$BP < \$100."
            continue
        fi

        # Get current price
        PRICE=$(python3 -c "
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
import os
client = StockHistoricalDataClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_SECRET_KEY'])
req = StockLatestTradeRequest(symbol_or_symbols='$SYM')
trade = client.get_stock_latest_trade(req)
print(float(trade['$SYM'].price))
" 2>/dev/null || echo "0")
        if python3 -c "exit(0 if float('$PRICE') <= 0 else 1)" 2>/dev/null; then
            log "  SKIP: could not get price."
            continue
        fi

        # Check existing position
        HOLDING=$(echo "$POSITIONS_JSON" | python3 -c "
import sys, json
positions = json.load(sys.stdin)
for p in positions:
    if p.get('symbol') == '$SYM':
        print(float(p.get('qty', 0)))
        exit()
print(0)
" 2>/dev/null || echo "0")

        if [ "$SIGNAL" = "BULLISH" ]; then
            if [ "$BUY_BLOCKED" = "1" ]; then
                log "  SKIP BUY: Fear & Greed blocked."
                continue
            fi
            if python3 -c "exit(0 if float('$HOLDING') > 0 else 1)" 2>/dev/null; then
                log "  SKIP BUY: already holding $HOLDING shares."
                continue
            fi
            # Position size: equity * 0.01 / (price * 0.02), adjusted by SIZE_MULT
            SHARES=$(python3 -c "
import math
equity = float('$EQUITY')
price  = float('$PRICE')
shares = math.floor(equity * 0.01 / (price * 0.02) * float('$SIZE_MULT'))
bp     = float('$BP')
# Also cap at 8% of equity
max_shares = int(equity * 0.08 / price)
shares = min(shares, max_shares)
if bp < price * shares:
    shares = int(bp * 0.95 / price)
print(max(0, shares))
" 2>/dev/null || echo "0")
            if python3 -c "exit(0 if int('$SHARES') == 0 else 1)" 2>/dev/null; then
                log "  SKIP BUY: 0 shares calculated."
                continue
            fi
            log "  → BUY $SHARES shares of $SYM @ ~\$$PRICE"
            python3 "$ALPACA" order buy "$SYM" "$SHARES" --force 2>&1 | tee -a "$LOG"
            # Update state
            python3 -c "
import json, os, time
sf = '$STATE_FILE'
d = json.load(open(sf)) if os.path.exists(sf) else {}
d.setdefault('trades', {})
d['trades']['$SYM'] = int(time.time())
json.dump(d, open(sf, 'w'))
" 2>/dev/null || true
            BP=$(python3 -c "print(max(0, float('$BP') - float('$PRICE') * int('$SHARES')))" 2>/dev/null || echo "$BP")

        elif [ "$SIGNAL" = "BEARISH" ]; then
            if python3 -c "exit(0 if float('$HOLDING') <= 0 else 1)" 2>/dev/null; then
                log "  SKIP SELL: no position in $SYM."
                continue
            fi
            SELL_QTY=$(python3 -c "print(int(float('$HOLDING')))" 2>/dev/null || echo "0")
            log "  → SELL $SELL_QTY shares of $SYM @ ~\$$PRICE"
            python3 "$ALPACA" order sell "$SYM" "$SELL_QTY" --force 2>&1 | tee -a "$LOG"
            python3 -c "
import json, os, time
sf = '$STATE_FILE'
d = json.load(open(sf)) if os.path.exists(sf) else {}
d.setdefault('trades', {})
d['trades']['$SYM'] = int(time.time())
json.dump(d, open(sf, 'w'))
" 2>/dev/null || true
        fi
    done
done < "$WATCHLIST"

log "=== Sentiment cycle complete ==="
