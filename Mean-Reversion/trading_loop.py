#!/usr/bin/env python3
"""
Mean-reversion autonomous trading loop with active exit management.

Flow per cycle:
  1. Refuse if market is closed.
  2. Pull account + positions.
  3. EXIT MANAGER runs first for every held name — uses mean_reversion.manage()
     with persisted entry/peak/age to fire take-profit, stop-loss, trailing-stop,
     SMA-target and time-stop exits. This is the "sells at the right time" fix.
  4. ENTRY SIGNALS for unheld names — Bollinger BUY in the lower 35% of the band.
  5. State (entries, peaks, timestamps) persists in trading_loop.state.json.

Env (auto-injected by run_trading_loop.sh):
  ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER=true
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE        = Path(__file__).resolve().parents[2]
MR_ENGINE        = WORKSPACE / "skills/Mean-Reversion/mean_reversion.py"
ALPACA_CLI       = WORKSPACE / "skills/ALPACA-TRADING/scripts/alpaca_cli.py"
LOG_FILE         = WORKSPACE / "skills/Mean-Reversion/trading_loop.log"
WATCHLIST_FILE   = WORKSPACE / "skills/Mean-Reversion/watchlist.default.txt"
STATE_FILE       = WORKSPACE / "skills/Mean-Reversion/trading_loop.state.json"

HOURLY_BARS      = 24       # 1 day of hourly bars for intraday BB (>=20 needed)
STARTING_EQUITY  = 100_000.0
MIN_BUYING_POWER = 100.0
MAX_POSITION_PCT = 0.15     # max 15% of equity per single name
DRAWDOWN_LIMIT   = 0.25     # halt if equity drops >25%
MAX_OPEN_POSITIONS = 6      # cap concurrent positions to control risk
# ──────────────────────────────────────────────────────────────────────────────


def _env() -> dict:
    return {
        **os.environ,
        "ALPACA_API_KEY":    os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER":      "true",
    }


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── State (persistent per-position metadata) ──────────────────────────────────


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"positions": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def minutes_since(iso_str: str) -> int:
    try:
        then = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - then
        return int(delta.total_seconds() / 60)
    except Exception:
        return 0


# ── Market hours / Alpaca SDK helpers ────────────────────────────────────────


def is_market_open() -> bool:
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True,
        )
        return client.get_clock().is_open
    except Exception as e:
        log(f"  clock check error: {e} — assuming open")
        return True


def get_account() -> dict | None:
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True,
        )
        acct = client.get_account()
        return {
            "equity":          float(acct.equity),
            "buying_power":    float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
        }
    except Exception as e:
        log(f"  account error: {e}")
        return None


def get_positions() -> dict[str, dict]:
    """Returns {symbol: {qty, avg_entry_price, current_price, unrealized_plpc}}."""
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True,
        )
        out: dict[str, dict] = {}
        for p in client.get_all_positions():
            out[p.symbol] = {
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
        return out
    except Exception as e:
        log(f"  positions error: {e}")
        return {}


def get_hourly_closes(symbol: str, n_bars: int = HOURLY_BARS) -> list[float]:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed

        client = StockHistoricalDataClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"],
        )
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Hour,
            start=start, end=end, feed=DataFeed.IEX,
        )
        bars = client.get_stock_bars(req)
        data = bars[symbol]
        closes = [float(b.close) for b in data]
        return closes[-n_bars:]
    except Exception as e:
        log(f"  hourly bars error ({symbol}): {e}")
        return []


def place_order(side: str, symbol: str, qty: int) -> bool:
    cmd = [sys.executable, str(ALPACA_CLI), "order", side, symbol, str(qty), "--force"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=_env(), cwd=str(WORKSPACE)
    )
    out = (result.stdout + result.stderr).strip()
    log(f"  ORDER {side.upper()} {qty}x {symbol}: {out[:300]}")
    return result.returncode == 0


# ── Engine wrappers (call mean_reversion.py JSON CLI) ────────────────────────


def _mr(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(MR_ENGINE), json.dumps(payload)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip()[:200]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "bad JSON"}


def mr_signal(closes: list[float], current_price: float) -> dict:
    return _mr({"action": "signal", "prices": closes, "current_price": current_price})


def mr_manage(entry, current, sma, lower, upper, peak, held_minutes) -> dict:
    return _mr({
        "action": "manage",
        "entry": entry, "current": current,
        "sma": sma, "lower": lower, "upper": upper,
        "peak": peak, "held_minutes": held_minutes,
    })


# ── Watchlist ────────────────────────────────────────────────────────────────


def load_watchlist() -> list[str]:
    symbols: list[str] = []
    for line in WATCHLIST_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for s in line.split(","):
            s = s.strip().upper()
            if s:
                symbols.append(s)
    return symbols


# ── Main loop ────────────────────────────────────────────────────────────────


def main() -> None:
    log("=== Mean-Reversion scan cycle start ===")

    if not is_market_open():
        log("Market is closed — skipping cycle.")
        return

    account = get_account()
    if not account:
        log("ABORT: could not fetch account.")
        return

    equity, buying_power = account["equity"], account["buying_power"]
    log(f"Equity: ${equity:,.2f}  |  Buying power: ${buying_power:,.2f}")

    drawdown_pct = (STARTING_EQUITY - equity) / STARTING_EQUITY * 100
    if drawdown_pct > DRAWDOWN_LIMIT * 100:
        log(f"ABORT: drawdown {drawdown_pct:.1f}% exceeds {DRAWDOWN_LIMIT*100:.0f}% limit.")
        return

    held = get_positions()
    state = load_state()
    state.setdefault("positions", {})

    # Reconcile state vs. broker reality (positions closed externally)
    for sym in list(state["positions"].keys()):
        if sym not in held:
            log(f"  state cleanup: {sym} no longer held — clearing local entry record.")
            del state["positions"][sym]

    # ── EXIT MANAGER (runs first to free buying power for new entries) ───────
    if held:
        log(f"Open positions: {list(held.keys())}")
    for sym, pos in list(held.items()):
        qty = int(pos["qty"])
        if qty <= 0:
            continue
        current_price = pos["current_price"]
        # Track / refresh peak
        meta = state["positions"].setdefault(
            sym,
            {
                "entry": pos["avg_entry_price"],
                "peak": current_price,
                "opened_at": now_iso(),
            },
        )
        meta["entry"] = meta.get("entry") or pos["avg_entry_price"]
        meta["peak"] = max(meta.get("peak", 0.0), current_price)

        closes = get_hourly_closes(sym)
        if len(closes) < 20:
            log(f"  EXIT-MGR {sym}: only {len(closes)} bars — using broker price only.")
            sma = current_price
            lower = upper = current_price
        else:
            from statistics import mean, pstdev
            window = closes[-20:]
            sma = mean(window)
            std = pstdev(window)
            upper, lower = sma + 2 * std, sma - 2 * std

        held_min = minutes_since(meta.get("opened_at", now_iso()))
        decision = mr_manage(
            entry=meta["entry"], current=current_price,
            sma=sma, lower=lower, upper=upper, peak=meta["peak"],
            held_minutes=held_min,
        )
        action = decision.get("action", "HOLD")
        log(
            f"  EXIT-MGR {sym}: pnl%={pos['unrealized_plpc']*100:+.2f}  "
            f"price=${current_price:.2f}  entry=${meta['entry']:.2f}  "
            f"peak=${meta['peak']:.2f}  sma=${sma:.2f}  held={held_min}m  "
            f"→ {action} ({decision.get('reason','')})"
        )
        if action != "HOLD":
            log(f"  → SELL {qty} {sym} ({action}) @ ~${current_price:.2f}")
            if place_order("sell", sym, qty):
                state["positions"].pop(sym, None)
                buying_power += qty * current_price
                held.pop(sym, None)
                save_state(state)

    # ── ENTRY SIGNALS ────────────────────────────────────────────────────────
    symbols = load_watchlist()
    log(f"Watchlist ({len(symbols)}): {symbols}")
    open_count = len(held)
    orders_placed = 0

    for sym in symbols:
        if sym in held:
            continue  # already have it; managed by exit logic above

        if open_count >= MAX_OPEN_POSITIONS:
            log(f"-- {sym}: SKIP (at max {MAX_OPEN_POSITIONS} open positions)")
            continue

        log(f"-- {sym}")

        closes = get_hourly_closes(sym)
        if len(closes) < 20:
            log(f"  SKIP: only {len(closes)} hourly bars (need >=20).")
            continue

        current_price = closes[-1]
        sig = mr_signal(closes, current_price)
        signal_kind = sig.get("signal", "HOLD")
        bands  = sig.get("bands", {})
        lower, upper, sma = bands.get("lower", 0), bands.get("upper", 0), bands.get("sma", 0)

        log(
            f"  hourly_bars={len(closes)}  signal={signal_kind}  "
            f"price=${current_price:.2f}  SMA=${sma:.2f}  "
            f"lower=${lower:.2f}  upper=${upper:.2f}"
        )

        if signal_kind != "BUY":
            continue

        if buying_power < MIN_BUYING_POWER:
            log(f"  SKIP BUY: buying power ${buying_power:.2f} < ${MIN_BUYING_POWER}")
            continue

        max_spend = min(equity * MAX_POSITION_PCT, buying_power * 0.95)
        qty = max(1, int(max_spend / current_price))
        if qty * current_price > buying_power:
            qty = max(0, int(buying_power * 0.95 / current_price))
        if qty <= 0:
            log("  SKIP BUY: zero qty after sizing.")
            continue

        log(f"  → BUYING {qty} {sym} @ ~${current_price:.2f} (≈${qty * current_price:,.2f})")
        if place_order("buy", sym, qty):
            buying_power -= qty * current_price
            open_count += 1
            orders_placed += 1
            state["positions"][sym] = {
                "entry": current_price,
                "peak": current_price,
                "opened_at": now_iso(),
            }
            save_state(state)

    log(f"=== Scan cycle complete: {orders_placed} new entry order(s) placed ===\n")


if __name__ == "__main__":
    main()
