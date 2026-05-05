---
name: mean-reversion
description: |
  Bollinger-band mean reversion workflow with Alpaca: watchlists, bar fetch,
  signals, guardrails, and dry-run reports. Use scan_report.py for a full
  no-trade scan; use alpaca skill for raw API CLI. Long-term default is daily bars.
---

# Mean reversion (Bollinger) + Alpaca — how to run it

There is **no single tool** named “run full scan cycle” in Cursor/OpenClaw. The workflow is **shell + two Python entrypoints**:

| Goal | What to run |
|------|----------------|
| **One-shot report (no orders)** | `python3 skills/Mean-Reversion/scan_report.py` |
| **Raw bars / account / watchlists** | `python3 skills/ALPACA-TRADING/scripts/alpaca_cli.py …` |
| **Signal math only (JSON)** | `python3 skills/Mean-Reversion/mean_reversion.py '<json>'` |

**Working directory:** OpenClaw agent workspace root (in Docker this is `/home/node/.openclaw/workspace`). On disk that is your **`.openclaw/workspace`** folder (you may mirror it under WSL for the gateway).

## Companion skill: sentiment-trader

**`skills/Sentiment-Trader/`** runs news/social sentiment + Alpaca execution on a **different schedule** (premarket + 15-minute cycles). Schedule **separate cron jobs** so sentiment and mean-reversion do not share one fragile prompt.

**Optional portfolio policy (your choice, does not weaken either skill):** e.g. only take a mean-reversion **BUY** when sentiment for that symbol is not **BEARISH**, or reduce size when sentiment is NEUTRAL. Both skills stay fully usable alone.

**Shared universe:** Prefer the same tickers as `watchlist.default.txt` here unless you deliberately split strategies.

**Paths (always from workspace root):**

- Alpaca CLI: `skills/ALPACA-TRADING/scripts/alpaca_cli.py`
- Signal engine: `skills/Mean-Reversion/mean_reversion.py`
- **Dry-run scan:** `skills/Mean-Reversion/scan_report.py`
- Default tickers file: `skills/Mean-Reversion/watchlist.default.txt`

---

## 1) Get the watchlist of tickers

**Option A — Alpaca account watchlists (authoritative if you maintain them there):**

```bash
cd skills/ALPACA-TRADING
python3 scripts/alpaca_cli.py watchlist list
```

Then run the scan using the first non-empty list:

```bash
cd ../..   # back to workspace root
python3 skills/Mean-Reversion/scan_report.py --use-alpaca-watchlist
```

**Option B — File-based list (good for long-term: stable universe, version-controlled):**

Edit `skills/Mean-Reversion/watchlist.default.txt` (comma-separated symbols, `#` comments allowed).

```bash
python3 skills/Mean-Reversion/scan_report.py
```

**Option C — Ad hoc symbols:**

```bash
python3 skills/Mean-Reversion/scan_report.py --symbols AAPL,MSFT,SPY
```

---

## 2) Fetch historical price data (e.g. last 20+ bars) per ticker

**Human-readable table:**

```bash
cd skills/ALPACA-TRADING
python3 scripts/alpaca_cli.py bars AAPL --timeframe 1Day --limit 25
```

**Machine-readable JSON (for agents/scripts):**

```bash
python3 scripts/alpaca_cli.py bars AAPL --timeframe 1Day --limit 25 --json
```

**Intraday example (not recommended as a *long-term* primary signal; noisier):**

```bash
python3 scripts/alpaca_cli.py bars AAPL --timeframe 5Min --limit 25 --json
```

`scan_report.py` calls `bars … --json` internally for each symbol.

---

## 3) Bollinger Bands + signals

Logic lives in `skills/Mean-Reversion/mean_reversion.py` (20-period SMA, ±2σ bands):

- **`bands`** — JSON bands from closes  
- **`signal`** — `BUY` / `SELL` / `HOLD` from last close vs bands  
- **`size`** — suggested share count from risk params (for live workflows only)  
- **`drawdown`** — compares starting vs current equity  

**Direct invocation (agent builds `prices` from `--json` bars):**

```bash
python3 skills/Mean-Reversion/mean_reversion.py '{"action":"bands","prices":[170,172,171,...]}'
python3 skills/Mean-Reversion/mean_reversion.py '{"action":"signal","prices":[...20+ closes...],"current_price":175.5}'
```

**End-to-end without wiring JSON yourself:**

```bash
python3 skills/Mean-Reversion/scan_report.py --timeframe 1Day --limit 25
```

Output includes `signal`, `bands`, and `bars_used` per symbol.

---

## 4) Guardrails

**In dry-run reports:** `scan_report.py` attaches `guardrails` and `would_trade` (false if any guard trips).

Default rules mirrored from your playbook:

- **BUY** skipped if `buying_power` &lt; `--min-buying-power` (default 500)  
- **BUY** skipped if already long the symbol  
- **SELL** skipped if no position  
- Optional **drawdown** line in report if you pass `--starting-equity` (same semantics as `mean_reversion.py` `drawdown` action)

**For any live order:** use `alpaca_cli.py order …` only after explicit human confirmation. Prefer **`ALPACA_PAPER=true`** until strategy is validated.

---

## 5) Report findings without executing trades

**Preferred — single JSON report (no orders):**

```bash
python3 skills/Mean-Reversion/scan_report.py --timeframe 1Day --limit 25
```

Optional:

```bash
python3 skills/Mean-Reversion/scan_report.py --use-alpaca-watchlist --starting-equity 100000
```

Pipe or save output for Discord/email summaries; **do not** call `order` subcommands in this mode.

**Account / positions JSON (for custom reports):**

```bash
cd skills/ALPACA-TRADING
python3 scripts/alpaca_cli.py account --json
python3 scripts/alpaca_cli.py positions --json
```

---

## Long-term / swing orientation (research-backed defaults)

- **Use daily (`1Day`) bars** as the default signal horizon for “investing-style” mean reversion; intraday bars increase turnover and false positives.  
- **Keep a small, liquid universe** (broad megacaps + index ETFs) to reduce gap/slippage risk versus thin names.  
- **Treat Bollinger touch as a filter, not prophecy** — combine with risk caps, paper trading, and avoiding over-leverage; past band touches do not guarantee future mean reversion.  
- **Run scans on a schedule you can stick to** (e.g. once daily after close, or pre-open), not necessarily every 5 minutes.  
- **Not financial advice** — you are responsible for compliance, taxes, and suitability.

---

## Autonomous / live cycle (only if you explicitly automate)

If you intentionally run a **live** bot on a short clock, use the **alpaca CLI** for `account`, `bars`, `order` and `mean_reversion.py` for JSON actions — never rely on `ALPACA-TRADING/scripts/mean_reversion.py` without reading its safety gate (legacy script that places orders; disabled unless `OPENCLAW_ALLOW_LEGACY_MEAN_REV_ALPACA_SCRIPT=1`).

---

## Credentials

Alpaca keys: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, optional `ALPACA_PAPER` — set in the environment for the process running the CLI (or `~/.openclaw/credentials/alpaca.json`). See the **alpaca** skill for details.

---

## If the gateway uses WSL paths

Edits under `C:\Users\harol\.openclaw\workspace\` should be mirrored to the WSL workspace mounted into Docker (e.g. `\\wsl.localhost\Ubuntu\home\<user>\.openclaw\workspace\`) so the container sees the same files.
