# Sentiment Data Sources

## Finnhub (primary)
- Endpoint: https://finnhub.io/api/v1/news-sentiment
- Provides: pre-scored bullish/bearish %, article buzz volume, sector comparison
- Insider: https://finnhub.io/api/v1/stock/insider-sentiment
- Provides: MSPR — monthly share purchase ratio
- Rate limit: 60 req/min free tier
- Key env var: FINNHUB_API_KEY

## Alpaca News (secondary)
- Endpoint: https://data.alpaca.markets/v1beta1/news
- Provides: recent headlines per ticker
- Rate limit: included with trading API
- Key env vars: ALPACA_API_KEY, ALPACA_SECRET_KEY

## Reddit (tertiary)
- Subreddits: wallstreetbets, stocks, investing
- No API key needed
- Posts weighted by upvote score

## CNN Fear & Greed
- Endpoint: https://production.dataviz.cnn.io/index/fearandgreed/graphdata
- Provides: 0–100 score, used as market regime filter
- No API key needed
- Rules:
  - score < 20: skip all new BUY orders
  - score > 80: reduce position size by 50%