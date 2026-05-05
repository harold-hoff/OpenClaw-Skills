import json
import sys
import math

BULLISH_WORDS = [
    "beat", "surge", "rally", "upgrade", "buy", "bullish", "growth",
    "profit", "record", "strong", "outperform", "breakout", "soar",
    "gain", "exceeds", "raises guidance", "partnership", "acquisition",
    "buyback", "dividend", "beat estimates", "revenue growth",
]

BEARISH_WORDS = [
    "miss", "drop", "fall", "downgrade", "sell", "bearish", "loss",
    "decline", "weak", "underperform", "crash", "plunge", "lawsuit",
    "investigation", "recall", "bankruptcy", "cuts guidance", "layoffs",
    "fraud", "subpoena", "missed estimates", "revenue decline",
]

IGNORE_WORDS = [
    "could", "might", "may", "rumor", "speculation",
    "expected", "potentially", "reportedly",
]


def score_text(text):
    text = text.lower()
    if any(w in text for w in IGNORE_WORDS):
        return 0
    bull = sum(1 for w in BULLISH_WORDS if w in text)
    bear = sum(1 for w in BEARISH_WORDS if w in text)
    return bull - bear


def analyze(data, ticker):
    score   = 0.0
    sources = []

    # --- Alpha Vantage AI score (premarket only, highest precision) ---
    av = data.get("av_sentiment", {})
    if av:
        av_score = av.get("av_sentiment_score", 0)
        av_count = av.get("av_article_count", 0)
        # AV scores range -1 to +1. Scale to match other sources.
        score   += av_score * 6.0 * min(av_count / 5.0, 2.0)
        sources.append("alpha_vantage_ai")

    # --- Finnhub pre-scored sentiment ---
    fh = data.get("finnhub_sentiment", {})
    if fh:
        bull_pct    = fh.get("bullish_pct", 0)
        bear_pct    = fh.get("bearish_pct", 0)
        buzz        = fh.get("buzz_articles", 0)
        sector_bull = fh.get("sector_bullish_pct", 0)
        net         = bull_pct - bear_pct
        buzz_mult   = min(buzz / 5.0, 3.0)
        score      += (net / 100.0) * 5.0 * buzz_mult
        if bull_pct > sector_bull + 10:
            score += 2
            sources.append("sector_outperform")
        sources.append("finnhub_sentiment")

    # --- Insider MSPR ---
    mspr = data.get("finnhub_insider", {}).get("mspr", 0)
    if mspr > 20:
        score += 3
        sources.append("insider_buying")
    elif mspr < -20:
        score -= 3
        sources.append("insider_selling")

    # --- News keyword scoring ---
    article_count = 0
    for article in data.get("news", []):
        text = article.get("title", "") + " " + article.get("summary", "")
        s    = score_text(text)
        if s != 0:
            score         += s * 0.5
            article_count += 1
            sources.append(article.get("source", "news"))

    # --- Reddit ---
    for post in data.get("reddit", []):
        s = score_text(post.get("title", ""))
        if s != 0:
            weight  = min(post.get("score", 1) / 500.0, 2.0)
            score  += s * weight
            sources.append(post.get("source", "reddit"))

    # --- Alpaca short-term price momentum (works when free-tier news APIs fail) ---
    am = data.get("alpaca_momentum", {})
    if am:
        mom_score = float(am.get("score", 0))
        score   += mom_score                          # already roughly [-3, +3]
        if mom_score:
            sources.append("alpaca_momentum")

    # --- Fear & Greed micro-adjustment (skip when missing) ---
    fg = data.get("fear_greed") or {}
    fg_score = fg.get("score") if isinstance(fg, dict) else None
    if isinstance(fg_score, (int, float)):
        score += (fg_score - 50) / 50.0
    else:
        fg_score = 50

    # --- Signal thresholds (lowered: free-tier news ⇒ thinner scores) ---
    if score >= 2.0:
        signal = "BULLISH"
    elif score <= -2.0:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    return {
        "ticker":        ticker,
        "signal":        signal,
        "score":         round(score, 2),
        "article_count": article_count,
        "sources":       list(set(sources)),
        "fear_greed":    fg_score,
        "insider_mspr":  mspr,
        "momentum":      am if am else None,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: sentiment_score.py TICKER '<json>'"}))
        sys.exit(1)
    ticker = sys.argv[1].upper()
    data   = json.loads(sys.argv[2])
    print(json.dumps(analyze(data, ticker)))