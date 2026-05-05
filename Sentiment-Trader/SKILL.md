---
name: sentiment-trader
description: Autonomous sentiment trading agent. Uses Finnhub, Alpha Vantage 
AI sentiment, NewsAPI, insider MSPR, and Reddit. Trades independently on 
Alpaca Paper Trading. Premarket scan at 09:15, then every 15 minutes.
---

# Sentiment Trading Agent

## Identity
You execute trades based solely on `sentiment_score.py` output after `news-fetcher.py` JSON.
Never override guardrails. Never trade on NEUTRAL signals.
Never rationalize a trade the rules would block.

## Canonical paths (workspace root)

Assume **`~/.openclaw/workspace`** (OpenClaw agent workspace). Do **not** use `alpaca-trading` (wrong casing on Linux).

| Purpose | Command |
|--------|---------|
| Alpaca CLI | `python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py …` |
| Fetch news bundle (JSON on stdout) | `python3 skills/Sentiment-Trader/scripts/news-fetcher.py TICKER` |
| Premarket bundle | `python3 skills/Sentiment-Trader/scripts/news-fetcher.py TICKER --premarket` |
| Score JSON → signal | `python3 skills/Sentiment-Trader/scripts/sentiment_score.py TICKER '<json>'` |

**Shell pattern (fetch pipe):**

```bash
SYMBOL=AAPL
RAW="$(python3 skills/Sentiment-Trader/scripts/news-fetcher.py "$SYMBOL")"
python3 skills/Sentiment-Trader/scripts/sentiment_score.py "$SYMBOL" "$RAW"
```

**Companion strategy:** Mean-reversion Bollinger scans live under **`skills/Mean-Reversion/`**. Keep tickers aligned with `skills/Mean-Reversion/watchlist.default.txt` unless you intentionally separate universes. Cron jobs for sentiment vs mean-reversion should be **separate** so one failure does not block the other.

**Optional confluence (does not replace either skill):** If both fire in the same session, you may require agreement (e.g. mean-reversion BUY only when sentiment signal is not BEARISH). That is **policy you layer on top** — do not delete or weaken either skill’s standalone rules.

## Environment Variables Required
- ALPACA_API_KEY
- ALPACA_SECRET_KEY
- FINNHUB_API_KEY
- NEWS_API_KEY
- ALPHA_VANTAGE_API_KEY
- ALPACA_PAPER=true

## Schedule
- 09:15 America/New_York: Premarket scan (uses all sources including Alpha Vantage + NewsAPI)
- Every 15 minutes 09:30–16:00 America/New_York: Standard cycle (Finnhub + Alpaca + Reddit only)
- 15:45 America/New_York: Close all positions

---

## 09:15 America/New_York Premarket Scan

Run once per day before market open. Consumes Alpha Vantage and NewsAPI quota.

For each ticker:

```bash
RAW="$(python3 skills/Sentiment-Trader/scripts/news-fetcher.py TICKER --premarket)"
python3 skills/Sentiment-Trader/scripts/sentiment_score.py TICKER "$RAW"
```

Store the resulting score and signal per ticker as the day's morning bias.
Log: `[09:15] TICKER | av_score:X | finnhub_score:X | morning_bias:BULLISH/BEARISH/NEUTRAL`

---

## Standard 15-Minute Cycle

### Step 1 — Check account and drawdown

```bash
python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py account --json
```

Parse `equity` vs session **starting equity** (you must persist start-of-day equity in session/state file or prior message context). If drawdown >= 5%: close all with sell orders, log HARD STOP, halt until next open.

Human-readable fallback:

```bash
python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py account
```

### Step 2 — Fear & Greed rules
- fear_greed < 20: skip all BUY orders this cycle
- fear_greed > 80: cut position sizes by 50%

### Step 3 — Fetch and score each ticker

```bash
RAW="$(python3 skills/Sentiment-Trader/scripts/news-fetcher.py TICKER)"
python3 skills/Sentiment-Trader/scripts/sentiment_score.py TICKER "$RAW"
```

If morning_bias exists for this ticker and current signal agrees: score += 1.5 bonus.
If morning_bias and current signal conflict: treat as NEUTRAL, skip.

### Step 4 — Guardrails (skip ticker if ANY true)
- signal == NEUTRAL
- article_count < 3
- buying_power < 500
- already holding this ticker
- ticker would exceed 20% of equity
- traded this ticker within last 60 minutes

### Step 5 — Position size
shares = floor(equity * 0.01 / (entry_price * 0.02))
If fear_greed > 80: shares = floor(shares * 0.5)
If shares == 0: skip.

### Step 6 — Execute
BULLISH:

```bash
python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py order buy TICKER SHARES --force
```

BEARISH (only if holding):

```bash
python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py order sell TICKER SHARES --force
```

### Step 7 — Monitor open positions
For each open position:
- unrealized_plpc >= 0.03: sell immediately (take profit)
- unrealized_plpc <= -0.02: sell immediately (stop loss)

Use:

```bash
python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py positions --json
```

### Step 8 — End of day
At 15:45 America/New_York: close all open positions.

### Step 9 — Log every ticker
`[HH:MM] TICKER | score:X | signal:X | mspr:X | fg:X | morning_bias:X | action:X | reason:X`
