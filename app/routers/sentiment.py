from fastapi import APIRouter, HTTPException, Query

from app.database import get_db
from app.sentiment_service import analyze_article, refresh_sentiment_for_ticker
from app.schemas import (
    SentimentArticleResponse,
    SentimentSummaryResponse,
    PortfolioSentimentItem,
    PortfolioSentimentResponse,
)

router = APIRouter(prefix="/sentiment", tags=["sentiment"])


@router.post("/analyze/{ticker}", response_model=list[SentimentArticleResponse])
def analyze_ticker_news(ticker: str, days: int = Query(7, ge=1, le=30)):
    with get_db() as conn:
        count = refresh_sentiment_for_ticker(ticker.upper(), conn, days)

    if count == 0:
        raise HTTPException(status_code=404, detail=f"No news found for {ticker.upper()}")

    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM news_articles
               WHERE ticker = ?
               ORDER BY analyzed_at DESC
               LIMIT ?""",
            (ticker.upper(), count),
        ).fetchall()
    return [_article_from_row(r) for r in rows]


@router.get("/{ticker}", response_model=list[SentimentArticleResponse])
def get_ticker_sentiment(ticker: str, limit: int = Query(20, ge=1, le=100)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM news_articles
               WHERE ticker = ?
               ORDER BY analyzed_at DESC
               LIMIT ?""",
            (ticker.upper(), limit),
        ).fetchall()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No analyzed articles for {ticker.upper()}. Call POST /sentiment/analyze/{ticker.upper()} first.",
        )
    return [_article_from_row(r) for r in rows]


@router.get("/{ticker}/summary", response_model=SentimentSummaryResponse)
def ticker_sentiment_summary(ticker: str, limit: int = Query(50, ge=1, le=200)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM news_articles
               WHERE ticker = ?
               ORDER BY analyzed_at DESC
               LIMIT ?""",
            (ticker.upper(), limit),
        ).fetchall()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No analyzed articles for {ticker.upper()}. Call POST /sentiment/analyze/{ticker.upper()} first.",
        )
    return _build_summary(ticker.upper(), rows)


@router.get("/users/{user_id}/portfolio-sentiment", response_model=PortfolioSentimentResponse)
def portfolio_sentiment(user_id: int):
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        holdings = conn.execute(
            "SELECT DISTINCT ticker FROM holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
        watchlist = conn.execute(
            "SELECT DISTINCT ticker FROM watchlist WHERE user_id = ?", (user_id,)
        ).fetchall()

    tickers = sorted({r["ticker"] for r in holdings} | {r["ticker"] for r in watchlist})
    if not tickers:
        raise HTTPException(status_code=404, detail="No holdings or watchlist items found")

    items: list[PortfolioSentimentItem] = []
    total_sentiment = 0.0
    tickers_with_data = 0

    with get_db() as conn:
        for ticker in tickers:
            rows = conn.execute(
                """SELECT sentiment_compound, sentiment_label FROM news_articles
                   WHERE ticker = ?
                   ORDER BY analyzed_at DESC
                   LIMIT 20""",
                (ticker,),
            ).fetchall()
            if rows:
                avg = sum(r["sentiment_compound"] for r in rows) / len(rows)
                total_sentiment += avg
                tickers_with_data += 1
                signal = _signal_from_avg(avg)
                items.append(PortfolioSentimentItem(
                    ticker=ticker, total_articles=len(rows), avg_sentiment=round(avg, 4), signal=signal
                ))
            else:
                items.append(PortfolioSentimentItem(
                    ticker=ticker, total_articles=0, avg_sentiment=0.0, signal="no_data"
                ))

    overall_avg = total_sentiment / tickers_with_data if tickers_with_data > 0 else 0.0
    overall_signal = _signal_from_avg(overall_avg) if tickers_with_data > 0 else "no_data"

    return PortfolioSentimentResponse(
        user_id=user_id, tickers=items, overall_signal=overall_signal
    )


def _signal_from_avg(avg: float) -> str:
    if avg >= 0.15:
        return "strong_bullish"
    if avg >= 0.05:
        return "bullish"
    if avg <= -0.15:
        return "strong_bearish"
    if avg <= -0.05:
        return "bearish"
    return "neutral"


def _article_from_row(row) -> SentimentArticleResponse:
    return SentimentArticleResponse(
        id=row["id"],
        ticker=row["ticker"],
        headline=row["headline"],
        source=row["source"],
        url=row["url"],
        summary=row["summary"],
        article_datetime=row["article_datetime"],
        sentiment_compound=row["sentiment_compound"],
        sentiment_label=row["sentiment_label"],
        headline_sentiment=row["headline_sentiment"],
        summary_sentiment=row["summary_sentiment"],
        analyzed_at=row["analyzed_at"],
    )


def _build_summary(ticker: str, rows: list) -> SentimentSummaryResponse:
    compounds = [r["sentiment_compound"] for r in rows if r["sentiment_compound"] is not None]
    avg = sum(compounds) / len(compounds) if compounds else 0.0
    bullish = sum(1 for r in rows if r["sentiment_label"] == "bullish")
    bearish = sum(1 for r in rows if r["sentiment_label"] == "bearish")
    neutral = sum(1 for r in rows if r["sentiment_label"] == "neutral")
    signal = _signal_from_avg(avg)
    latest = [_article_from_row(r) for r in rows[:5]]
    return SentimentSummaryResponse(
        ticker=ticker,
        total_articles=len(rows),
        avg_sentiment=round(avg, 4),
        bullish_count=bullish,
        bearish_count=bearish,
        neutral_count=neutral,
        signal=signal,
        latest_articles=latest,
    )
