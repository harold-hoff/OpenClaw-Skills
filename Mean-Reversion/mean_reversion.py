"""Mean-reversion signal + position-management engine.

CLI usage (single JSON arg):
    python3 mean_reversion.py '{"action":"signal","prices":[...],"current_price":X}'
    python3 mean_reversion.py '{"action":"manage","entry":X,"current":X,"sma":X,"lower":X,"peak":X,"held_minutes":N}'
    python3 mean_reversion.py '{"action":"size","equity":X,"entry":X,"lower_band":X}'
    python3 mean_reversion.py '{"action":"drawdown","starting_equity":X,"current_equity":X}'

Returns JSON. The `manage` action is the key exit-timing helper — it answers
"should I sell this open position right now?" so the loop is no longer just
good-at-buying-bad-at-selling.
"""

import json
import sys

import numpy as np


def bollinger_bands(prices, period=20):
    arr = np.array(prices[-period:], dtype=float)
    sma = float(np.mean(arr))
    std = float(np.std(arr))
    return {
        "sma":   round(sma, 4),
        "upper": round(sma + 2 * std, 4),
        "lower": round(sma - 2 * std, 4),
        "std":   round(std, 4),
    }


def position_size(equity, entry, lower_band, risk_pct=0.01):
    risk_amount = equity * risk_pct
    stop_loss = lower_band - (entry - lower_band)  # mirror below entry
    per_share_risk = entry - stop_loss
    if per_share_risk <= 0:
        return 0
    return int(risk_amount / per_share_risk)


def check_drawdown(starting_equity, current_equity, threshold=0.05):
    drawdown = (starting_equity - current_equity) / starting_equity
    return {
        "drawdown_pct": round(drawdown * 100, 2),
        "should_stop":  drawdown >= threshold,
    }


def signal(current_price, bands):
    """Mean-reversion ENTRY signals.

    BUY  – price is in lower 35% of the band (oversold, fade the dip).
    SELL – price is in upper 10% of the band (rare overbought standalone short).
    HOLD – everything else.

    Note: most exits are produced by the `manage` action, not by waiting for
    the upper band to be tagged after a BUY entry.
    """
    lower = bands["lower"]
    upper = bands["upper"]
    band_range = upper - lower
    if band_range <= 0:
        return "HOLD"
    if current_price <= lower + 0.35 * band_range:
        return "BUY"
    if current_price >= upper - 0.10 * band_range:
        return "SELL"
    return "HOLD"


# Exit thresholds — tightened 2026-05-05 after observing positions bleeding all
# afternoon at -1.4% never triggering the old -2.0% stop. With shorter time
# windows we book small wins faster and cut bleeders before they hit -2%.
TAKE_PROFIT_PCT      = 0.008   # +0.8% from entry: take profit (was 1.2%)
STOP_LOSS_PCT        = 0.012   # -1.2% from entry: cut loss   (was 2.0%)
TRAILING_STOP_PCT    = 0.006   # 0.6% give-back from peak     (was 1.0%)
TRAILING_ARM_PCT     = 0.004   # arm trailing at +0.4% gain   (was 0.7%)
TIME_STOP_MINUTES    = 120     # paper: avoid flattening flat mean-reversion legs right before a move
SOFT_EXIT_MINUTES    = 45      # after 45m, accept a small loss if below SMA
SOFT_EXIT_PNL        = -0.004  # if pnl <= -0.4% AND below SMA: SOFT_EXIT
SMA_TARGET_TOLERANCE = 0.0015  # tag SMA target within 0.15%  (was 0.1%)
MIN_GAIN_TO_HOLD_PAST_TIME = 0.001  # small green enough to hold past time-stop (was +0.3%)


def manage(entry, current, sma, lower=None, upper=None, peak=None, held_minutes=0):
    """Decide whether to exit an open long position. Returns:

    {"action":"HOLD"|"TAKE_PROFIT"|"STOP_LOSS"|"TRAILING_STOP"|"TIME_STOP"
     |"BAND_BREAK"|"SOFT_EXIT", "reason": str}
    """
    entry = float(entry)
    current = float(current)
    sma = float(sma) if sma is not None else current
    peak = float(peak) if peak else current
    held_minutes = int(held_minutes or 0)

    if entry <= 0:
        return {"action": "HOLD", "reason": "no entry price"}

    pnl_pct = (current - entry) / entry

    # 1. Hard stop loss — protect capital first
    if pnl_pct <= -STOP_LOSS_PCT:
        return {"action": "STOP_LOSS",
                "reason": f"pnl {pnl_pct:.2%} <= -{STOP_LOSS_PCT:.2%}"}

    # 2. Band break — strategy invalidated when price falls well below lower band
    if lower is not None and current < float(lower) * 0.99:
        return {"action": "BAND_BREAK",
                "reason": f"price ${current:.2f} < lower ${float(lower):.2f} - 1%"}

    # 3. Take profit at small target — we live off many small wins
    if pnl_pct >= TAKE_PROFIT_PCT:
        return {"action": "TAKE_PROFIT",
                "reason": f"pnl {pnl_pct:.2%} >= +{TAKE_PROFIT_PCT:.2%}"}

    # 4. Reached SMA target (mean reverted) — book it
    if abs(current - sma) / max(sma, 1e-9) <= SMA_TARGET_TOLERANCE and pnl_pct > 0:
        return {"action": "TAKE_PROFIT", "reason": f"reverted to SMA ${sma:.2f}"}

    # 5. Trailing stop once armed
    if pnl_pct >= TRAILING_ARM_PCT:
        peak_pnl = (peak - entry) / entry
        give_back = (peak - current) / max(peak, 1e-9)
        if peak_pnl >= TRAILING_ARM_PCT and give_back >= TRAILING_STOP_PCT:
            return {
                "action": "TRAILING_STOP",
                "reason": f"gave back {give_back:.2%} from peak ${peak:.2f} "
                          f"(peak_pnl {peak_pnl:.2%})",
            }

    # 6. Soft exit — small loss + mean already broken below us → no edge left
    if held_minutes >= SOFT_EXIT_MINUTES and pnl_pct <= SOFT_EXIT_PNL and current < sma:
        return {"action": "SOFT_EXIT",
                "reason": f"held {held_minutes}m at {pnl_pct:.2%} below SMA ${sma:.2f}"}

    # 7. Time stop — don't hold trades that aren't working
    if held_minutes >= TIME_STOP_MINUTES and pnl_pct < MIN_GAIN_TO_HOLD_PAST_TIME:
        return {
            "action": "TIME_STOP",
            "reason": f"held {held_minutes}m, pnl {pnl_pct:.2%} below "
                      f"+{MIN_GAIN_TO_HOLD_PAST_TIME:.2%} hold-past-time gate",
        }

    return {"action": "HOLD", "reason": f"pnl {pnl_pct:.2%}, no exit triggered"}


# CLI entry point
if __name__ == "__main__":
    data = json.loads(sys.argv[1])
    action = data["action"]

    if action == "bands":
        print(json.dumps(bollinger_bands(data["prices"])))

    elif action == "signal":
        bands = bollinger_bands(data["prices"])
        sig = signal(data["current_price"], bands)
        print(json.dumps({"signal": sig, "bands": bands}))

    elif action == "manage":
        result = manage(
            entry=data.get("entry"),
            current=data.get("current"),
            sma=data.get("sma"),
            lower=data.get("lower"),
            upper=data.get("upper"),
            peak=data.get("peak"),
            held_minutes=data.get("held_minutes", 0),
        )
        print(json.dumps(result))

    elif action == "size":
        size = position_size(data["equity"], data["entry"], data["lower_band"])
        print(json.dumps({"shares": size}))

    elif action == "drawdown":
        result = check_drawdown(data["starting_equity"], data["current_equity"])
        print(json.dumps(result))

    else:
        print(json.dumps({"error": f"unknown action: {action}"}))
        sys.exit(2)
