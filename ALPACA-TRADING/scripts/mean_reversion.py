"""
Legacy mean-reversion scanner that places real Alpaca orders.

Disabled by default. Prefer:
  - skills/Mean-Reversion/scan_report.py  (dry-run, JSON report)
  - skills/Mean-Reversion/mean_reversion.py  (pure signal math, JSON CLI)

To run this file anyway (not recommended): OPENCLAW_ALLOW_LEGACY_MEAN_REV_ALPACA_SCRIPT=1
"""
import os
import time
import sys
import pandas as pd
import ta
import warnings
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Ignore pandas FutureWarnings related to boolean indexing
warnings.simplefilter(action='ignore', category=FutureWarning)

api_key = os.environ.get('ALPACA_API_KEY')
api_secret = os.environ.get('ALPACA_SECRET_KEY') # NOTE: It's ALPACA_SECRET_KEY in env, not ALPACA_API_SECRET
paper = os.environ.get('ALPACA_PAPER', 'true').lower() == 'true'

trading_client = TradingClient(api_key, api_secret, paper=paper)
data_client = StockHistoricalDataClient(api_key, api_secret)

symbols = ['AAPL', 'TSLA', 'MSFT', 'NVDA', 'SPY', 'QQQ', 'AMD', 'META', 'AMZN', 'GOOGL']

def check_mean_reversion():
    print("Checking mean reversion opportunities...")
    
    positions = {p.symbol: p for p in trading_client.get_all_positions()}
    account = trading_client.get_account()
    buying_power = float(account.buying_power)
    
    for symbol in symbols:
        try:
            # Get historical daily data
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                limit=100
            )
            bars = data_client.get_stock_bars(request).df
            
            if bars.empty:
                continue
                
            # Calculate technical indicators
            bars['rsi'] = ta.momentum.RSIIndicator(close=bars['close'], window=14).rsi()
            bars['bollinger_hband'] = ta.volatility.BollingerBands(close=bars['close'], window=20, window_dev=2).bollinger_hband()
            bars['bollinger_lband'] = ta.volatility.BollingerBands(close=bars['close'], window=20, window_dev=2).bollinger_lband()
            bars['sma_20'] = ta.trend.SMAIndicator(close=bars['close'], window=20).sma_indicator()
            
            latest = bars.iloc[-1]
            current_price = latest['close']
            
            print(f"{symbol}: Price=${current_price:.2f}, RSI={latest['rsi']:.2f}")
            
            # Very oversold condition (Mean Reversion Buy)
            if latest['rsi'] < 30 and current_price < latest['bollinger_lband']:
                print(f"OVERSOLD SIGNAL ON {symbol}! (RSI: {latest['rsi']:.2f})")
                if symbol not in positions and buying_power > current_price * 10:
                    qty = 10
                    print(f"--> Submitting BUY order for {qty} shares of {symbol}")
                    market_order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.GTC
                    )
                    trading_client.submit_order(order_data=market_order_data)
                    buying_power -= (current_price * qty) # Estimate
            
            # Very overbought condition (Mean Reversion Sell/Short)
            elif latest['rsi'] > 70 and current_price > latest['bollinger_hband']:
                print(f"OVERBOUGHT SIGNAL ON {symbol}! (RSI: {latest['rsi']:.2f})")
                if symbol in positions and float(positions[symbol].qty) > 0:
                    qty = float(positions[symbol].qty)
                    print(f"--> Submitting SELL order to exit {symbol} position")
                    market_order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    )
                    trading_client.submit_order(order_data=market_order_data)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")

if __name__ == "__main__":
    if os.environ.get("OPENCLAW_ALLOW_LEGACY_MEAN_REV_ALPACA_SCRIPT", "").strip() != "1":
        print(
            "Refusing to run: this script places LIVE orders via Alpaca.\n"
            "For scans without orders use: python3 skills/Mean-Reversion/scan_report.py\n"
            "To force this legacy script (not recommended): export OPENCLAW_ALLOW_LEGACY_MEAN_REV_ALPACA_SCRIPT=1",
            file=sys.stderr,
        )
        sys.exit(2)
    check_mean_reversion()

