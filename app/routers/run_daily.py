from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_db
from app.finnhub_service import get_quote
from app.recommendation_engine import generate_recommendations

router = APIRouter(prefix="/run-daily", tags=["run-daily"])


class RunDailyResponse(BaseModel):
    user_id: int
    tickers: list[str]
    prices_refreshed: int
    articles_analyzed: int
    recommendations_generated: int
    errors: list[str]


@router.post("/{user_id}", response_model=RunDailyResponse)
def run_daily(user_id: int):
    """Run the full daily analysis pipeline for a user:
    refresh prices → analyze sentiment → generate recommendations → send WhatsApp.
    """
    errors: list[str] = []

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, name, phone_number FROM users WHERE id = ?", (user_id,)
        ).fetchone()
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
        raise HTTPException(
            status_code=400,
            detail="No holdings or watchlist items. Add stocks before running daily analysis.",
        )

    # --- Step 1: Refresh prices ---
    prices_refreshed = 0
    with get_db() as conn:
        for ticker in tickers:
            try:
                data = get_quote(ticker)
                if data:
                    conn.execute(
                        """INSERT INTO price_snapshots
                           (ticker, current_price, change, percent_change, high, low, open, previous_close)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            data["ticker"],
                            data["current_price"],
                            data.get("change"),
                            data.get("percent_change"),
                            data.get("high"),
                            data.get("low"),
                            data.get("open"),
                            data.get("previous_close"),
                        ),
                    )
                    prices_refreshed += 1
            except Exception as e:
                errors.append(f"Price fetch failed for {ticker}: {e}")

    # --- Step 2 & 3: Sentiment refresh + recommendations (done together inside generate_recommendations) ---
    # generate_recommendations calls refresh_sentiment_for_ticker internally, so
    # we track article counts by querying news_articles after the fact.
    articles_before = _count_recent_articles(tickers)

    try:
        recs = generate_recommendations(user_id)
    except Exception as e:
        errors.append(f"Recommendation generation failed: {e}")
        recs = []

    articles_after = _count_recent_articles(tickers)
    articles_analyzed = max(0, articles_after - articles_before)

    return RunDailyResponse(
        user_id=user_id,
        tickers=tickers,
        prices_refreshed=prices_refreshed,
        articles_analyzed=articles_analyzed,
        recommendations_generated=len(recs),
        errors=errors,
    )


def _count_recent_articles(tickers: list[str]) -> int:
    if not tickers:
        return 0
    placeholders = ",".join("?" * len(tickers))
    with get_db() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM news_articles WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchone()
    return row["cnt"] if row else 0
