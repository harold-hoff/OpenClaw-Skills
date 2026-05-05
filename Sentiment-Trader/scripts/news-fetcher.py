import requests
import json
import sys
import os
from datetime import date, datetime, timedelta, timezone

ALPACA_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_KEY   = os.getenv("FINNHUB_API_KEY")
NEWS_API_KEY  = os.getenv("NEWS_API_KEY")
AV_KEY        = os.getenv("ALPHA_VANTAGE_API_KEY")


def fetch_finnhub_sentiment(ticker):
    try:
        url = f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=5).json()
        return {
            "bullish_pct":        r.get("sentiment", {}).get("bullishPercent", 0),
            "bearish_pct":        r.get("sentiment", {}).get("bearishPercent", 0),
            "buzz_articles":      r.get("buzz", {}).get("articlesInLastWeek", 0),
            "buzz_weekly_avg":    r.get("buzz", {}).get("weeklyAverage", 0),
            "sector_bullish_pct": r.get("sectorAverageBullishPercent", 0),
        }
    except:
        return {}


def fetch_finnhub_insider(ticker):
    try:
        start = (date.today() - timedelta(days=90)).isoformat()
        url = (
            f"https://finnhub.io/api/v1/stock/insider-sentiment"
            f"?symbol={ticker}&from={start}&token={FINNHUB_KEY}"
        )
        data = requests.get(url, timeout=5).json().get("data", [])
        if not data:
            return {"mspr": 0}
        return {"mspr": data[-1].get("mspr", 0)}
    except:
        return {"mspr": 0}


def fetch_finnhub_news(ticker):
    try:
        today    = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        url = (
            f"https://finnhub.io/api/v1/company-news"
            f"?symbol={ticker}&from={week_ago}&to={today}&token={FINNHUB_KEY}"
        )
        articles = requests.get(url, timeout=5).json()[:10]
        return [
            {
                "title":   a.get("headline", ""),
                "summary": a.get("summary", ""),
                "source":  "finnhub_news",
            }
            for a in articles
        ]
    except:
        return []


def fetch_alpaca_news(ticker):
    try:
        url     = "https://data.alpaca.markets/v1beta1/news"
        headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        r = requests.get(
            url, headers=headers,
            params={"symbols": ticker, "limit": 5},
            timeout=5
        ).json()
        return [
            {
                "title":   a["headline"],
                "summary": a.get("summary", ""),
                "source":  "alpaca_news",
            }
            for a in r.get("news", [])
        ]
    except:
        return []


def fetch_newsapi(ticker):
    """
    NewsAPI — 100 req/day free.
    Call only during pre-market scan and midday scan.
    Broader web coverage than Finnhub — catches news Finnhub misses.
    """
    if not NEWS_API_KEY:
        return []
    try:
        url = "https://newsapi.org/v2/everything"
        params = {
            "q":        ticker,
            "language": "en",
            "sortBy":   "publishedAt",
            "pageSize": 10,
            "apiKey":   NEWS_API_KEY,
        }
        articles = requests.get(url, params=params, timeout=5).json().get("articles", [])
        return [
            {
                "title":   a.get("title", ""),
                "summary": a.get("description", ""),
                "source":  "newsapi",
            }
            for a in articles
            if a.get("title")
        ]
    except:
        return []


def fetch_alpha_vantage_sentiment(ticker):
    """
    Alpha Vantage AI-scored news sentiment — 25 req/day free.
    Call ONLY during pre-market scan (09:15 EST). Never in 15-min cycles.
    Returns a pre-computed score — zero keyword parsing needed.
    """
    if not AV_KEY:
        return {}
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers":  ticker,
            "limit":    10,
            "apikey":   AV_KEY,
        }
        r = requests.get(url, params=params, timeout=8).json()
        feed = r.get("feed", [])
        if not feed:
            return {}

        scores = []
        for article in feed:
            for ts in article.get("ticker_sentiment", []):
                if ts.get("ticker") == ticker:
                    try:
                        scores.append(float(ts.get("ticker_sentiment_score", 0)))
                    except:
                        pass

        if not scores:
            return {}

        avg = sum(scores) / len(scores)
        return {
            "av_sentiment_score": round(avg, 4),
            "av_article_count":   len(scores),
        }
    except:
        return {}


def fetch_reddit(ticker):
    results = []
    headers = {"User-Agent": "openclaw-sentiment-bot/1.0"}
    for sub in ["wallstreetbets", "stocks", "investing"]:
        try:
            url = (
                f"https://www.reddit.com/r/{sub}/search.json"
                f"?q={ticker}&sort=new&limit=5&t=day"
            )
            posts = requests.get(url, headers=headers, timeout=5).json()[
                "data"
            ]["children"]
            for p in posts:
                d = p["data"]
                results.append(
                    {
                        "title":  d["title"],
                        "score":  d["score"],
                        "source": f"reddit_{sub}",
                    }
                )
        except:
            continue
    return results


def fetch_fear_greed():
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            timeout=5,
        ).json()
        return {
            "score":  r["fear_and_greed"]["score"],
            "rating": r["fear_and_greed"]["rating"],
        }
    except:
        return {"score": 50, "rating": "neutral"}


def fetch_alpaca_momentum(ticker):
    """
    Pure-Alpaca short-term momentum proxy (works even when Finnhub/CNN block us).
    Score in roughly [-3, +3]: +1 per ~0.4% positive move (1h + ~6h), capped.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed

        if not (ALPACA_KEY and ALPACA_SECRET):
            return {"error": "alpaca creds missing"}

        client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        req = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Hour,
            start=start, end=end, feed=DataFeed.IEX,
        )
        bars_resp = client.get_stock_bars(req)
        rows = bars_resp[ticker] if ticker in bars_resp.data else []
        closes = [float(b.close) for b in rows]
        if len(closes) < 2:
            return {"error": f"only {len(closes)} bars"}
        last = closes[-1]
        prev_1h = closes[-2]
        prior = closes[-min(7, len(closes))]
        chg_1h = (last - prev_1h) / prev_1h * 100 if prev_1h else 0.0
        chg_session = (last - prior) / prior * 100 if prior else 0.0

        def bucket(pct):
            return max(-1.5, min(1.5, pct / 0.4))

        score = bucket(chg_1h) + bucket(chg_session)
        return {
            "score":              round(score, 3),
            "change_1h_pct":      round(chg_1h, 3),
            "change_session_pct": round(chg_session, 3),
            "last_price":         last,
            "bars_used":          len(closes),
        }
    except Exception as e:
        return {"error": str(e)[:120]}


def fetch_all(ticker, premarket=False):
    """
    premarket=True: full scan including Alpha Vantage and NewsAPI.
    premarket=False: Finnhub + Alpaca + Reddit + price-momentum (15-min cycle).
    """
    result = {
        "finnhub_sentiment": fetch_finnhub_sentiment(ticker),
        "finnhub_insider":   fetch_finnhub_insider(ticker),
        "news":              fetch_finnhub_news(ticker) + fetch_alpaca_news(ticker),
        "reddit":            fetch_reddit(ticker),
        "fear_greed":        fetch_fear_greed(),
        "av_sentiment":      {},
        "alpaca_momentum":   fetch_alpaca_momentum(ticker),
    }

    if premarket:
        result["news"]        += fetch_newsapi(ticker)
        result["av_sentiment"] = fetch_alpha_vantage_sentiment(ticker)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: news_fetcher.py TICKER [--premarket]"}))
        sys.exit(1)
    ticker    = sys.argv[1].upper()
    premarket = "--premarket" in sys.argv
    print(json.dumps(fetch_all(ticker, premarket=premarket)))