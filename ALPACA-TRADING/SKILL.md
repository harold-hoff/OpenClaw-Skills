---
name: alpaca
description: Trade US stocks via Alpaca API. Use for market data (quotes, bars, news), placing orders (market, limit, stop), checking positions, portfolio management, and account info. Supports both paper and live trading. Use when user asks about stock prices, wants to buy/sell securities, check portfolio, or manage trades.
---

# Alpaca Trading Skill

Trade US equities programmatically via Alpaca's stock trading API (this CLI does not use Alpaca's crypto endpoints).

## Setup

Install the Python client (required by `scripts/alpaca_cli.py`):

```bash
pip install alpaca-py
```

### Credentials

Set **`ALPACA_API_KEY`** and **`ALPACA_SECRET_KEY`** (and usually **`ALPACA_PAPER=true`**) in the environment for the shell or container that runs the CLI. Optional file: `~/.openclaw/credentials/alpaca.json` with `apiKey`, `secretKey`, `paper`.

**Note:** A repo-level `.env` is not auto-loaded by `alpaca_cli.py` — export vars in your compose file, shell profile, or gateway environment.

### Mean reversion / Bollinger scans (no orders)

For a **full dry-run** (watchlist → bars → bands → signals → guardrails → JSON), use the **mean-reversion** skill:

```bash
# From workspace root (~/.openclaw/workspace)
python3 skills/Mean-Reversion/scan_report.py --timeframe 1Day --limit 25
```

## Quick Reference

### Get Quote
```bash
python3 scripts/alpaca_cli.py quote AAPL
python3 scripts/alpaca_cli.py quote AAPL,TSLA,NVDA
```
Shows raw bid/ask plus **Ref $** (sane NBBO midpoint, otherwise **last sale**). Free market-data feeds often publish incomplete or absurd NBBO; use **Ref $** for fractional sizing — not the raw ask when it is zero or wildly wide.

### Get Bars (Historical Data)
```bash
python3 scripts/alpaca_cli.py bars AAPL --timeframe 1Day --limit 10
python3 scripts/alpaca_cli.py bars AAPL --timeframe 1Hour --start 2026-02-01
# JSON for scripts / agents (closes in .bars[].close)
python3 scripts/alpaca_cli.py bars AAPL --timeframe 1Day --limit 25 --json
```

### Check Account
```bash
python3 scripts/alpaca_cli.py account
python3 scripts/alpaca_cli.py account --json
```

### List Positions
```bash
python3 scripts/alpaca_cli.py positions
python3 scripts/alpaca_cli.py positions --json
```

### Place Orders
```bash
# Market order
python3 scripts/alpaca_cli.py order buy AAPL 10

# Limit order
python3 scripts/alpaca_cli.py order buy AAPL 10 --limit 150.00

# Stop order
python3 scripts/alpaca_cli.py order sell TSLA 5 --stop 200.00

# Stop-limit order
python3 scripts/alpaca_cli.py order sell TSLA 5 --stop 200.00 --limit 195.00

# Skip price validation (use with caution)
python3 scripts/alpaca_cli.py order buy AAPL 10 --limit 999.00 --force
```

**Order Guardrails:**

1. **Symbol validation** — Rejects invalid/unknown tickers
2. **Buying power check** — Blocks orders exceeding available funds, shows max shares
3. **Duplicate detection** — Warns if you have open orders for same symbol/side
4. **Price validation** — Warns if limit price is worse than market
5. **Market hours check** — Detects pre-market, after-hours, and closed sessions
   - Pre-market (4:00 AM - 9:30 AM ET): Option to place pre-market order
   - After-hours (4:00 PM - 8:00 PM ET): Option to place after-hours order
   - Closed: Warns order will queue until market open
6. **Cost confirmation** — Shows total cost and requires confirmation

Use `--force` to skip all confirmation prompts (use with caution).

### List Orders
```bash
python3 scripts/alpaca_cli.py orders
python3 scripts/alpaca_cli.py orders --status open
python3 scripts/alpaca_cli.py orders --status closed --limit 20
```

### Cancel Order
```bash
python3 scripts/alpaca_cli.py cancel ORDER_ID
python3 scripts/alpaca_cli.py cancel all  # Cancel all open orders
```

### Get News
```bash
python3 scripts/alpaca_cli.py news AAPL
python3 scripts/alpaca_cli.py news AAPL,TSLA --limit 5
```

### Watchlist
```bash
python3 scripts/alpaca_cli.py watchlist list
python3 scripts/alpaca_cli.py watchlist create --name "Tech Stocks" --symbols AAPL,MSFT,GOOGL
python3 scripts/alpaca_cli.py watchlist add --watchlist_id WATCHLIST_ID --symbol NVDA
python3 scripts/alpaca_cli.py watchlist delete --watchlist_id WATCHLIST_ID
```

### Stream Live Data (Websocket)
```bash
# Stream trades (default)
python3 scripts/alpaca_cli.py stream AAPL

# Stream quotes
python3 scripts/alpaca_cli.py stream AAPL,TSLA --type quotes

# Stream bars (1-min)
python3 scripts/alpaca_cli.py stream NVDA --type bars

# Stream all data types
python3 scripts/alpaca_cli.py stream AAPL --type all
```

Press Ctrl+C to stop streaming.

### Price Alerts
```bash
# Add alert - notify when INTU drops below $399
python3 scripts/alpaca_cli.py alert add --symbol INTU --price 399 --condition below

# Add alert - notify when AAPL goes above $300
python3 scripts/alpaca_cli.py alert add --symbol AAPL --price 300 --condition above

# List active alerts
python3 scripts/alpaca_cli.py alert list

# Check alerts (used by cron)
python3 scripts/alpaca_cli.py alert check

# Remove an alert
python3 scripts/alpaca_cli.py alert remove --alert_id ABC123

# Clear all alerts
python3 scripts/alpaca_cli.py alert clear
```

Alerts are stored in `~/.openclaw/data/alpaca-alerts.json`.

## Script Location

Run commands with **`cd` into this skill directory** (`skills/ALPACA-TRADING`) so `scripts/alpaca_cli.py` resolves correctly, **or** pass the absolute path to `alpaca_cli.py` from the workspace root.

## API Reference

See `references/api.md` for detailed API documentation and response formats.

## Safety Notes

- Always confirm with user before placing real trades
- Paper trading (`ALPACA_PAPER=true`) recommended for testing
- Check buying power before large orders
- Verify order details before submission
