#!/usr/bin/env python3
"""
Dry-run mean-reversion scan: watchlist → bars → Bollinger signal → guardrails → JSON report.
Does NOT place orders. Uses ALPACA-TRADING/scripts/alpaca_cli.py for market data + account state.

Run from workspace root (recommended):
  python3 skills/Mean-Reversion/scan_report.py

Or from this directory:
  python3 scan_report.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _skill_root() -> Path:
    return Path(__file__).resolve().parent


def _workspace_root() -> Path:
    # skills/Mean-Reversion -> workspace
    return _skill_root().parent.parent


def _alpaca_cli() -> Path:
    return _workspace_root() / "skills" / "ALPACA-TRADING" / "scripts" / "alpaca_cli.py"


def _mean_reversion_py() -> Path:
    return _skill_root() / "mean_reversion.py"


def _run_cli(argv: List[str]) -> Tuple[int, str, str]:
    cli = _alpaca_cli()
    if not cli.is_file():
        return 127, "", f"alpaca_cli not found at {cli}"
    proc = subprocess.run(
        [sys.executable, str(cli), *argv],
        capture_output=True,
        text=True,
        cwd=str(cli.parent),
        env=os.environ.copy(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_mean_reversion(payload: dict) -> dict:
    mr = _mean_reversion_py()
    proc = subprocess.run(
        [sys.executable, str(mr), json.dumps(payload)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip() or proc.stdout.strip() or "mean_reversion failed"}
    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": "invalid JSON from mean_reversion", "raw": proc.stdout}


def _load_default_watchlist() -> List[str]:
    p = _skill_root() / "watchlist.default.txt"
    if not p.is_file():
        return []
    out: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.replace(";", ",").split(","):
            s = part.strip().upper()
            if s:
                out.append(s)
    return out


def _symbols_from_alpaca_watchlist() -> Optional[List[str]]:
    code, out, err = _run_cli(["watchlist", "list"])
    if code != 0:
        return None
    # Lines like: uuid: Name [SYM1, SYM2, ...]
    for line in out.splitlines():
        m = re.search(r"\[([^\]]+)\]", line)
        if m:
            raw = m.group(1)
            syms = [x.strip().upper() for x in raw.split(",") if x.strip()]
            if syms:
                return syms
    return None


def _account_json() -> Optional[dict]:
    code, out, err = _run_cli(["account", "--json"])
    if code != 0:
        return None
    try:
        return json.loads(out.strip())
    except json.JSONDecodeError:
        return None


def _positions_json() -> List[dict]:
    code, out, err = _run_cli(["positions", "--json"])
    if code != 0:
        return []
    try:
        data = json.loads(out.strip())
        return list(data.get("positions") or [])
    except json.JSONDecodeError:
        return []


def _bars_json(symbol: str, timeframe: str, limit: int) -> Optional[dict]:
    code, out, err = _run_cli(
        ["bars", symbol, "--timeframe", timeframe, "--limit", str(limit), "--json"]
    )
    if code != 0:
        return None
    try:
        return json.loads(out.strip())
    except json.JSONDecodeError:
        return None


def _drawdown_state(account: dict, starting_equity: Optional[float]) -> dict:
    if not starting_equity or starting_equity <= 0:
        return {
            "skipped": True,
            "reason": "No --starting-equity; pass prior day equity to enforce daily drawdown stop in reports.",
        }
    cur = float(account.get("equity", 0))
    dd = _run_mean_reversion(
        {
            "action": "drawdown",
            "starting_equity": starting_equity,
            "current_equity": cur,
        }
    )
    return dd


def _guardrails(
    symbol: str,
    signal: str,
    account: dict,
    positions_by_symbol: Dict[str, dict],
    min_buying_power: float,
    max_single_name_pct: float,
    *,
    sizing: Optional[Dict[str, Any]] = None,
) -> List[str]:
    reasons: List[str] = []
    bp = float(account.get("buying_power", 0))
    eq = float(account.get("equity", 0))

    if signal == "BUY":
        if bp < min_buying_power:
            reasons.append(f"buying_power ${bp:.2f} < min ${min_buying_power:.2f}")
        if symbol in positions_by_symbol:
            reasons.append("already_long; skip duplicate BUY")
        elif (
            sizing is not None
            and eq > 0
            and max_single_name_pct > 0
        ):
            shares = int(sizing.get("shares") or 0)
            px = float(sizing.get("entry") or 0)
            if shares > 0 and px > 0:
                notion = shares * px
                if notion / eq > max_single_name_pct:
                    reasons.append(
                        f"sized_notional {notion/eq:.1%} of equity > cap {max_single_name_pct:.0%}"
                    )
    if signal == "SELL":
        if symbol not in positions_by_symbol:
            reasons.append("no position; SELL not applicable")

    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run Bollinger mean-reversion scan (no orders)."
    )
    parser.add_argument(
        "--symbols",
        help="Comma-separated symbols (overrides watchlist)",
    )
    parser.add_argument(
        "--use-alpaca-watchlist",
        action="store_true",
        help="Use first non-empty Alpaca account watchlist (else watchlist.default.txt)",
    )
    parser.add_argument(
        "--timeframe",
        default="1Day",
        help="Alpaca bar timeframe (1Day recommended for swing / long-term)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Bars to fetch (need >=20 for 20-period Bollinger)",
    )
    parser.add_argument(
        "--starting-equity",
        type=float,
        default=None,
        help="Prior reference equity for drawdown % in report (optional)",
    )
    parser.add_argument(
        "--min-buying-power",
        type=float,
        default=500.0,
        help="Guardrail: min buying power to allow BUY in report",
    )
    parser.add_argument(
        "--max-position-pct",
        type=float,
        default=0.20,
        help="Guardrail: max fraction of equity for one name (informational on BUY)",
    )
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.use_alpaca_watchlist:
        symbols = _symbols_from_alpaca_watchlist() or []
        if not symbols:
            symbols = _load_default_watchlist()
    else:
        symbols = _load_default_watchlist()

    if not symbols:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "No symbols: set watchlist.default.txt, use --use-alpaca-watchlist, or --symbols",
                },
                indent=2,
            )
        )
        return 1

    account = _account_json()
    positions = _positions_json()
    pos_map = {p["symbol"]: p for p in positions}

    rows: List[dict] = []
    for sym in symbols:
        bar_pack = _bars_json(sym, args.timeframe, args.limit)
        if not bar_pack or bar_pack.get("error") == "no_data":
            rows.append({"symbol": sym, "error": "no_bars"})
            continue
        bars = bar_pack.get("bars") or []
        closes = [float(b["close"]) for b in bars]
        if len(closes) < 20:
            rows.append(
                {
                    "symbol": sym,
                    "error": f"insufficient_bars need>=20 got={len(closes)}",
                    "closes_count": len(closes),
                }
            )
            continue
        current = closes[-1]
        sig = _run_mean_reversion(
            {"action": "signal", "prices": closes, "current_price": current}
        )
        signal = sig.get("signal", "HOLD")
        bands = sig.get("bands", {})
        sizing_payload: Optional[dict] = None
        if signal == "BUY" and account and bands:
            sz = _run_mean_reversion(
                {
                    "action": "size",
                    "equity": float(account.get("equity", 0)),
                    "entry": current,
                    "lower_band": float(bands.get("lower", 0)),
                }
            )
            if isinstance(sz, dict) and "shares" in sz:
                sizing_payload = {
                    "shares": int(sz.get("shares", 0)),
                    "entry": current,
                }
        gr = []
        if account:
            gr = _guardrails(
                sym,
                signal,
                account,
                pos_map,
                args.min_buying_power,
                args.max_position_pct,
                sizing=sizing_payload,
            )
        rows.append(
            {
                "symbol": sym,
                "timeframe": args.timeframe,
                "bars_used": len(closes),
                "current_price": current,
                "signal": signal,
                "bands": bands,
                "suggested_shares": sizing_payload["shares"] if sizing_payload else None,
                "guardrails": gr,
                "would_trade": signal in ("BUY", "SELL") and not gr,
            }
        )

    report: Dict[str, Any] = {
        "ok": True,
        "mode": "dry_run_no_orders",
        "timeframe": args.timeframe,
        "symbols": symbols,
        "per_symbol": rows,
    }
    if account:
        report["account"] = {
            "equity": account.get("equity"),
            "buying_power": account.get("buying_power"),
            "portfolio_value": account.get("portfolio_value"),
            "pattern_day_trader": account.get("pattern_day_trader"),
        }
        report["drawdown_check"] = _drawdown_state(account, args.starting_equity)
    else:
        report["account"] = None
        report["drawdown_check"] = {"skipped": True, "reason": "account --json failed"}

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
