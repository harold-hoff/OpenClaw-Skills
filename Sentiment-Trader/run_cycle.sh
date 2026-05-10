#!/usr/bin/env bash
# Sentiment-Trader cycle: scan universe, sell BEARISH first, then BUY BULLISH
# sorted by score (highest conviction uses buying power first — fixes alphabetical
# starvation e.g. QQQ getting $0 after AMD/GOOGL/AMZN).
#
# Env (optional):
#   MAX_SENTIMENT_BUYS_PER_CYCLE  max new BUYs per cycle (default 6)
#   MIN_BULLISH_SCORE             skip BULLISH below this score (default 0)
#   SENTIMENT_MAX_EQUITY_FRAC     max notional per name as fraction of equity (default 0.12)

set -uo pipefail

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
WATCHLIST="$WORKSPACE/skills/Mean-Reversion/watchlist.default.txt"
NEWS="$WORKSPACE/skills/Sentiment-Trader/scripts/news-fetcher.py"
SCORE="$WORKSPACE/skills/Sentiment-Trader/scripts/sentiment_score.py"
ALPACA="$WORKSPACE/skills/ALPACA-TRADING/scripts/alpaca_cli.py"
LOG="$WORKSPACE/skills/Sentiment-Trader/cycle.log"
STATE_FILE="$WORKSPACE/skills/Sentiment-Trader/state.json"

MAX_BUYS="${MAX_SENTIMENT_BUYS_PER_CYCLE:-6}"
MIN_BULL="${MIN_BULLISH_SCORE:-0}"
MAX_EQ_FRAC="${SENTIMENT_MAX_EQUITY_FRAC:-0.12}"

export ALPACA_API_KEY="${ALPACA_API_KEY:-}"
export ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-}"
export ALPACA_PAPER="${ALPACA_PAPER:-true}"
export FINNHUB_API_KEY="${FINNHUB_API_KEY:-}"
export NEWS_API_KEY="${NEWS_API_KEY:-}"
export ALPHA_VANTAGE_API_KEY="${ALPHA_VANTAGE_API_KEY:-}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

NOW_TS=$(date +%s)
log "=== Sentiment cycle start (sorted BULLISH; max buys/cycle=$MAX_BUYS) ==="

# --- Account ---
ACCT_JSON=$(python3 "$ALPACA" account --json 2>/dev/null || echo '{}')
EQUITY=$(echo "$ACCT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('equity',100000))" 2>/dev/null || echo "100000")
BP=$(echo "$ACCT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('buying_power',0))" 2>/dev/null || echo "0")
log "Equity: \$$EQUITY | Buying power: \$$BP"

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

DRAWDOWN=$(python3 -c "print(($START_EQUITY - $EQUITY) / $START_EQUITY * 100)" 2>/dev/null || echo "0")
if python3 -c "exit(0 if float('$DRAWDOWN') >= 20 else 1)" 2>/dev/null; then
    log "HARD STOP: drawdown ${DRAWDOWN}% >= 20%. Closing all positions."
    python3 "$ALPACA" positions --json 2>/dev/null | python3 -c "
import sys, json, subprocess
raw = json.load(sys.stdin)
pl = raw.get('positions', raw) if isinstance(raw, dict) else raw
if not isinstance(pl, list):
    pl = []
for p in pl:
    sym = p.get('symbol','')
    qty = int(float(p.get('qty',0)))
    if qty > 0:
        subprocess.run(['python3', '$ALPACA', 'order', 'sell', sym, str(qty), '--force'])
" 2>/dev/null || true
    exit 0
fi

FG_RAW=$(timeout 15 python3 "$NEWS" AAPL 2>/dev/null || echo '{}')
FEAR_GREED=$(echo "$FG_RAW" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('fear_greed',{}).get('score',50))" 2>/dev/null || echo "50")
log "Fear & Greed: $FEAR_GREED"

if python3 -c "exit(0 if float('$FEAR_GREED') < 10 else 1)" 2>/dev/null; then
    log "Fear & Greed < 10: skipping all BUY orders this cycle."
    BUY_BLOCKED=1
else
    BUY_BLOCKED=0
fi

SIZE_MULT=$(python3 -c "print(0.5 if float('$FEAR_GREED') > 80 else 1.0)" 2>/dev/null || echo "1.0")

positions_json() {
    python3 "$ALPACA" positions --json 2>/dev/null || echo '{"positions":[]}'
}

holding_qty() {
    local sym="$1"
    local pj="$2"
    echo "$pj" | python3 -c "
import json, sys
raw = json.load(sys.stdin)
pl = raw.get('positions', raw) if isinstance(raw, dict) else raw
if not isinstance(pl, list):
    pl = []
for p in pl:
    if p.get('symbol') == sys.argv[1]:
        print(float(p.get('qty', 0)))
        sys.exit(0)
print(0)
" "$sym"
}

refresh_account() {
    ACCT_JSON=$(python3 "$ALPACA" account --json 2>/dev/null || echo '{}')
    EQUITY=$(echo "$ACCT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('equity',0))" 2>/dev/null || echo "0")
    BP=$(echo "$ACCT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('buying_power',0))" 2>/dev/null || echo "0")
}

POSITIONS_JSON=$(positions_json)

SCAN=$(mktemp)
trap 'rm -f "$SCAN"' EXIT

# --- Phase 1: scan tickers → tab rows: symbol, signal, score, articles, price, holding, cooldown_ok ---
while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [[ -z "$line" ]] && continue
    IFS=',' read -ra TICKERS <<< "$line"
    for SYM in "${TICKERS[@]}"; do
        SYM="$(echo "$SYM" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')"
        [[ -z "$SYM" ]] && continue

        log "-- $SYM"

        LAST_TRADE=$(python3 -c "
import json, os
sf = '$STATE_FILE'
if os.path.exists(sf):
    d = json.load(open(sf))
    print(d.get('trades', {}).get('$SYM', 0))
else:
    print(0)
" 2>/dev/null || echo "0")
        SINCE=$(( NOW_TS - ${LAST_TRADE:-0} ))
        COOL_OK=1
        if [ "$SINCE" -lt 900 ]; then
            log "  SKIP active signals: traded ${SYM} ${SINCE}s ago (15-min cooldown)."
            COOL_OK=0
        fi

        RAW=$(timeout 20 python3 "$NEWS" "$SYM" 2>/dev/null || echo '{}')
        RESULT=$(echo "$RAW" | python3 "$SCORE" "$SYM" "$RAW" 2>/dev/null || echo '{"signal":"NEUTRAL","score":0,"article_count":0}')

        SIGNAL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('signal','NEUTRAL'))" 2>/dev/null || echo "NEUTRAL")
        ARTICLE_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('article_count',0))" 2>/dev/null || echo "0")
        SCORE_VAL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('score',0))" 2>/dev/null || echo "0")

        log "  signal=$SIGNAL score=$SCORE_VAL articles=$ARTICLE_COUNT"

        if [ "$SIGNAL" = "NEUTRAL" ]; then
            continue
        fi

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

        HOLDING=$(holding_qty "$SYM" "$POSITIONS_JSON")

        # signal, score, symbol, articles, price, holding, cooldown_ok (signal first for sort stability)
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$SIGNAL" "$SCORE_VAL" "$SYM" "$ARTICLE_COUNT" "$PRICE" "$HOLDING" "$COOL_OK" >>"$SCAN"
    done
done < "$WATCHLIST"

# --- Phase 2: BEARISH sells (any order) ---
while IFS=$'\t' read -r SIGNAL SCORE_VAL SYM ARTICLE_COUNT PRICE HOLDING COOL_OK; do
    [ "$SIGNAL" = "BEARISH" ] || continue
    if python3 -c "exit(0 if float('$HOLDING') <= 0 else 1)" 2>/dev/null; then
        log "  [SELL phase] $SYM: skip — no position."
        continue
    fi
    SELL_QTY=$(python3 -c "print(int(float('$HOLDING')))" 2>/dev/null || echo "0")
    log "  → SELL $SELL_QTY $SYM @ ~\$$PRICE (BEARISH)"
    python3 "$ALPACA" order sell "$SYM" "$SELL_QTY" --force 2>&1 | tee -a "$LOG"
    python3 -c "
import json, os, time
sf = '$STATE_FILE'
d = json.load(open(sf)) if os.path.exists(sf) else {}
d.setdefault('trades', {})
d['trades']['$SYM'] = int(time.time())
json.dump(d, open(sf, 'w'))
" 2>/dev/null || true
done <"$SCAN"

refresh_account
POSITIONS_JSON=$(positions_json)

# --- Phase 3: BULLISH buys, highest score first ---
BUYS_DONE=0
while IFS=$'\t' read -r SIGNAL SCORE_VAL SYM ARTICLE_COUNT PRICE HOLDING COOL_OK; do
    [ "$SIGNAL" = "BULLISH" ] || continue

    if [ "$BUYS_DONE" -ge "$MAX_BUYS" ]; then
        log "  [BUY phase] $SYM: skip — reached MAX_SENTIMENT_BUYS_PER_CYCLE ($MAX_BUYS)."
        continue
    fi

    if python3 -c "exit(0 if float('$SCORE_VAL') < float('$MIN_BULL') else 1)" 2>/dev/null; then
        log "  [BUY phase] $SYM: skip — score $SCORE_VAL < MIN_BULLISH_SCORE ($MIN_BULL)."
        continue
    fi

    if [ "$COOL_OK" != "1" ]; then
        log "  [BUY phase] $SYM: skip — cooldown."
        continue
    fi

    if [ "$BUY_BLOCKED" = "1" ]; then
        log "  [BUY phase] $SYM: skip — Fear & Greed blocked."
        continue
    fi

    PRICE=$(python3 -c "
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
import os
client = StockHistoricalDataClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_SECRET_KEY'])
req = StockLatestTradeRequest(symbol_or_symbols='$SYM')
trade = client.get_stock_latest_trade(req)
print(float(trade['$SYM'].price))
" 2>/dev/null || echo "0")

    HOLDING=$(holding_qty "$SYM" "$POSITIONS_JSON")
    if python3 -c "exit(0 if float('$HOLDING') > 0 else 1)" 2>/dev/null; then
        log "  [BUY phase] $SYM: skip — already holding."
        continue
    fi

    if python3 -c "exit(0 if float('$BP') < 100 else 1)" 2>/dev/null; then
        log "  [BUY phase] $SYM: skip — buying power \$$BP < \$100."
        continue
    fi

    SHARES=$(python3 -c "
import math
equity = float('$EQUITY')
price  = float('$PRICE')
frac = float('$MAX_EQ_FRAC')
mult = float('$SIZE_MULT')
bp     = float('$BP')
shares = math.floor(equity * 0.01 / (price * 0.02) * mult)
max_shares = int(equity * frac / price)
shares = min(shares, max_shares)
if bp < price * shares:
    shares = int(bp * 0.98 / price)
print(max(0, shares))
" 2>/dev/null || echo "0")

    if python3 -c "exit(0 if int('$SHARES') < 1 else 1)" 2>/dev/null; then
        log "  [BUY phase] $SYM: skip — 0 shares (BP or caps)."
        continue
    fi

    log "  → BUY $SHARES $SYM @ ~\$$PRICE (score=$SCORE_VAL)"
    python3 "$ALPACA" order buy "$SYM" "$SHARES" --force 2>&1 | tee -a "$LOG"
    python3 -c "
import json, os, time
sf = '$STATE_FILE'
d = json.load(open(sf)) if os.path.exists(sf) else {}
d.setdefault('trades', {})
d['trades']['$SYM'] = int(time.time())
json.dump(d, open(sf, 'w'))
" 2>/dev/null || true

    BUYS_DONE=$((BUYS_DONE + 1))
    refresh_account
    POSITIONS_JSON=$(positions_json)
done < <(grep '^BULLISH' "$SCAN" | sort -t$'\t' -k2,2gr)

log "=== Sentiment cycle complete ($BUYS_DONE buy(s)) ==="
