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
import traceback
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
MAX_POSITION_PCT = 0.12     # paper: slightly larger per-name sleeve (was 10%)
DRAWDOWN_LIMIT   = 0.25     # halt if equity drops >25%
MAX_OPEN_POSITIONS = 6      # paper: allow broader book alongside sentiment sleeve
MAX_NEW_ENTRIES_PER_CYCLE = 2   # can add two MR legs per daemon pass when signals stack
RESERVE_CASH_PCT = 0.05     # use more BP on paper; keep 5% cushion for exits
# Skip marginal BUYs at the noisy edge of the band — improves expectancy vs churn.
MIN_ENTRY_DEPTH = 0.05    # (SMA−price)/band_range must be ≥ this (deeper dip)

# End-of-day failsafe — if any cycle runs at/after this NY-time minute,
# liquidate everything before evaluating new signals. Backstops the
# 15:50 ET cron job in case it crashes/misses.
EOD_FLATTEN_HOUR_ET = 15    # 3:xx PM ET
EOD_FLATTEN_MIN_ET  = 50    # at minute 50
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


def _is_after_eod_flatten() -> bool:
    """True if NY clock is past 15:50 ET (or whatever EOD_FLATTEN_* is set to)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        return (now.hour, now.minute) >= (EOD_FLATTEN_HOUR_ET, EOD_FLATTEN_MIN_ET)
    except Exception:
        return False


def _eod_already_flattened_today() -> bool:
    """Check the dated EOD flag file so we don't re-flatten on every cycle."""
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        flag = WORKSPACE / "skills/Mean-Reversion/eod.flag"
        if not flag.exists():
            return False
        return flag.read_text().strip() == today
    except Exception:
        return False


def _mark_eod_flattened_today() -> None:
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        flag = WORKSPACE / "skills/Mean-Reversion/eod.flag"
        flag.write_text(today)
    except Exception:
        pass


def _flatten_all(held: dict, state: dict, reason: str) -> None:
    """Sell every long, cover every short. Used by intraday EOD failsafe."""
    log(f"!!! EOD FAILSAFE FLATTEN ({reason}): closing {len(held)} positions !!!")
    for sym, pos in list(held.items()):
        qty = int(float(pos.get("qty", 0)))
        if qty == 0:
            continue
        side = "sell" if qty > 0 else "buy"
        log(f"  → {side.upper()} {abs(qty)} {sym} (EOD)")
        if place_order(side, sym, abs(qty)):
            state["positions"].pop(sym, None)
    save_state(state)
    _mark_eod_flattened_today()


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

    # ── EOD FAILSAFE ────────────────────────────────────────────────────────
    # If we're past 15:50 ET and cron's close-all-positions-eod failed/crashed,
    # liquidate everything and skip the cycle. Idempotent: dated flag file
    # ensures we only attempt once per trading day.
    if _is_after_eod_flatten():
        if _eod_already_flattened_today():
            log("Past EOD flatten time and flag set — refusing new entries; nothing to do.")
            return
        if held:
            _flatten_all(held, state, reason="cron EOD missed or after market wind-down")
        else:
            _mark_eod_flattened_today()
            log("Past EOD flatten time but no positions held — flag set, exiting.")
        return

    # Reconcile state vs. broker reality (positions closed externally)
    for sym in list(state["positions"].keys()):
        if sym not in held:
            log(f"  state cleanup: {sym} no longer held — clearing local entry record.")
            del state["positions"][sym]

    # ── EXIT MANAGER (runs first to free buying power for new entries) ───────
    if held:
        log(f"Open positions: {list(held.keys())}")
    for sym, pos in list(held.items()):
        try:
            qty = int(float(pos["qty"]))
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
        except Exception as e:
            log(f"  EXIT-MGR {sym}: ERROR (skipped): {e}")

    # ── ENTRY SIGNALS ────────────────────────────────────────────────────────
    symbols = load_watchlist()
    log(f"Watchlist ({len(symbols)}): {symbols}")
    open_count = len(held)
    orders_placed = 0

    # Cash buffer: never spend the last ~10% of equity on new entries; that
    # cushion absorbs price slippage on stops and prevents BP=$0 lockups.
    bp_floor = max(MIN_BUYING_POWER, equity * RESERVE_CASH_PCT)
    if buying_power <= bp_floor:
        log(f"-- ENTRY: buying power ${buying_power:.2f} <= reserve ${bp_floor:.2f}; "
            "skipping new entries this cycle.")
        log(f"=== Scan cycle complete: 0 new entry order(s) placed ===\n")
        return

    # Score every candidate first so we pick the *best* setup of this cycle,
    # not just the first one in alphabetical order.
    candidates = []
    for sym in symbols:
        try:
            if sym in held:
                continue
            if open_count >= MAX_OPEN_POSITIONS:
                log(f"-- {sym}: SKIP (at max {MAX_OPEN_POSITIONS} open positions)")
                continue

            closes = get_hourly_closes(sym)
            if len(closes) < 20:
                log(f"-- {sym}: SKIP (only {len(closes)} hourly bars; need >=20)")
                continue

            current_price = closes[-1]
            sig = mr_signal(closes, current_price)
            signal_kind = sig.get("signal", "HOLD")
            bands = sig.get("bands", {})
            lower = bands.get("lower", 0)
            upper = bands.get("upper", 0)
            sma   = bands.get("sma", 0)

            if signal_kind != "BUY":
                log(f"-- {sym}: signal={signal_kind} (no buy)")
                continue

            # Strength score: how deep into the lower half of the band we are.
            band_range = max(upper - lower, 1e-9)
            depth = (sma - current_price) / band_range
            if depth < MIN_ENTRY_DEPTH:
                log(f"-- {sym}: SKIP BUY depth={depth:.3f} < {MIN_ENTRY_DEPTH} (too shallow)")
                continue
            log(f"-- {sym}: BUY candidate price=${current_price:.2f} "
                f"SMA=${sma:.2f} lower=${lower:.2f} depth={depth:+.3f}")
            candidates.append((depth, sym, current_price, lower, upper, sma))
        except Exception as e:
            log(f"-- {sym}: ENTRY scan ERROR (skipped): {e}")

    candidates.sort(reverse=True)  # deepest first

    for depth, sym, current_price, lower, upper, sma in candidates:
        try:
            if orders_placed >= MAX_NEW_ENTRIES_PER_CYCLE:
                log(f"-- {sym}: SKIP (already placed {orders_placed} new entries this cycle)")
                continue
            if open_count >= MAX_OPEN_POSITIONS:
                log(f"-- {sym}: SKIP (at max {MAX_OPEN_POSITIONS} open positions)")
                continue

            spend_cap = min(equity * MAX_POSITION_PCT, (buying_power - bp_floor) * 0.95)
            if spend_cap <= 0:
                log(f"-- {sym}: SKIP BUY (no spend cap left after reserve)")
                continue

            qty = int(spend_cap / current_price)
            if qty * current_price > (buying_power - bp_floor):
                qty = int((buying_power - bp_floor) * 0.95 / current_price)
            if qty <= 0:
                log(f"-- {sym}: SKIP BUY (zero qty after sizing).")
                continue

            log(f"  → BUYING {qty} {sym} @ ~${current_price:.2f} "
                f"(≈${qty * current_price:,.2f})  depth={depth:+.3f}")
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
        except Exception as e:
            log(f"-- {sym}: BUY ERROR (skipped): {e}")

    log(f"=== Scan cycle complete: {orders_placed} new entry order(s) placed ===\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"=== Cycle FATAL (logged, exiting cleanly): {e} ===")
        log(traceback.format_exc())
        sys.exit(0)
