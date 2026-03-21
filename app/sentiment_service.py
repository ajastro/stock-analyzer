import sqlite3

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()


def analyze_text(text: str) -> dict:
    if not text or not text.strip():
        return {"compound": 0.0, "positive": 0.0, "negative": 0.0, "neutral": 0.0, "label": "neutral"}
    scores = _analyzer.polarity_scores(text)
    compound = scores["compound"]
    if compound >= 0.05:
        label = "bullish"
    elif compound <= -0.05:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "compound": round(compound, 4),
        "positive": round(scores["pos"], 4),
        "negative": round(scores["neg"], 4),
        "neutral": round(scores["neu"], 4),
        "label": label,
    }


def analyze_article(headline: str, summary: str) -> dict:
    h_scores = analyze_text(headline)
    s_scores = analyze_text(summary)
    combined = h_scores["compound"] * 0.6 + s_scores["compound"] * 0.4
    if combined >= 0.05:
        label = "bullish"
    elif combined <= -0.05:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "compound": round(combined, 4),
        "headline_sentiment": h_scores["compound"],
        "summary_sentiment": s_scores["compound"],
        "label": label,
    }


def refresh_sentiment_for_ticker(ticker: str, conn: sqlite3.Connection, days: int = 7) -> int:
    """Fetch latest news for ticker, analyze sentiment, upsert into news_articles.

    Returns the number of articles processed. Silently skips on API errors so
    callers (e.g. the recommendation engine) can continue without crashing.
    """
    from app.finnhub_service import get_company_news  # local import avoids circular deps

    try:
        articles = get_company_news(ticker, days)
    except Exception:
        return 0

    count = 0
    for a in articles:
        url = a.get("url", "")
        if not url:
            continue
        sentiment = analyze_article(a.get("headline", ""), a.get("summary", ""))
        existing = conn.execute(
            "SELECT id FROM news_articles WHERE ticker = ? AND url = ?",
            (ticker.upper(), url),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE news_articles
                   SET sentiment_compound = ?, sentiment_label = ?,
                       headline_sentiment = ?, summary_sentiment = ?,
                       analyzed_at = datetime('now')
                   WHERE id = ?""",
                (
                    sentiment["compound"],
                    sentiment["label"],
                    sentiment["headline_sentiment"],
                    sentiment["summary_sentiment"],
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO news_articles
                   (ticker, headline, source, url, summary, article_datetime,
                    sentiment_compound, sentiment_label, headline_sentiment, summary_sentiment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticker.upper(),
                    a.get("headline", ""),
                    a.get("source", ""),
                    url,
                    a.get("summary", ""),
                    a.get("datetime", 0),
                    sentiment["compound"],
                    sentiment["label"],
                    sentiment["headline_sentiment"],
                    sentiment["summary_sentiment"],
                ),
            )
        count += 1
    return count
