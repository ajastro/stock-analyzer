from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.database import get_db
from app.recommendation_engine import generate_recommendations
from app.schemas import (
    RecommendationResponse,
    GenerateRecommendationsResponse,
)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.post("/generate/{user_id}", response_model=GenerateRecommendationsResponse)
def generate(user_id: int):
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        holdings = conn.execute(
            "SELECT ticker FROM holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
        watchlist = conn.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ?", (user_id,)
        ).fetchall()
        all_tickers = set(r["ticker"] for r in holdings) | set(r["ticker"] for r in watchlist)
        if not all_tickers:
            raise HTTPException(
                status_code=400,
                detail="No holdings or watchlist items. Add stocks before generating recommendations.",
            )

        today = datetime.utcnow().strftime("%Y-%m-%d")
        budget_row = conn.execute(
            "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
            (user_id, today),
        ).fetchone()
        if budget_row:
            remaining_budget = (
                budget_row["base_budget"]
                + budget_row["sell_profits"]
                - budget_row["amount_spent"]
            )
        else:
            remaining_budget = 100.0

    results = generate_recommendations(user_id)
    recs = [
        RecommendationResponse(
            user_id=r["user_id"],
            ticker=r["ticker"],
            signal=r["signal"],
            combined_score=r["combined_score"],
            price_score=r["price_score"],
            technical_score=r["technical_score"],
            sentiment_score=r["sentiment_score"],
            current_price=r["current_price"],
            reason=r["reason"],
            affordable_shares=r["affordable_shares"],
            unrealized_gain_loss=r["unrealized_gain_loss"],
        )
        for r in results
    ]
    return GenerateRecommendationsResponse(
        user_id=user_id,
        generated_count=len(recs),
        remaining_budget=round(remaining_budget, 2),
        recommendations=recs,
    )


@router.get("/{user_id}", response_model=list[RecommendationResponse])
def get_latest_recommendations(user_id: int):
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        latest = conn.execute(
            "SELECT MAX(generated_at) as latest FROM recommendations WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not latest or not latest["latest"]:
            raise HTTPException(
                status_code=404,
                detail="No recommendations yet. Call POST /recommendations/generate/{user_id} first.",
            )

        rows = conn.execute(
            """SELECT * FROM recommendations
               WHERE user_id = ? AND generated_at = ?
               ORDER BY ABS(combined_score) DESC""",
            (user_id, latest["latest"]),
        ).fetchall()
    return [_rec_from_row(r) for r in rows]


@router.get("/{user_id}/history", response_model=list[RecommendationResponse])
def get_recommendation_history(
    user_id: int,
    ticker: str = Query(None, min_length=1),
    limit: int = Query(50, ge=1, le=200),
):
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if ticker:
            rows = conn.execute(
                """SELECT * FROM recommendations
                   WHERE user_id = ? AND ticker = ?
                   ORDER BY generated_at DESC
                   LIMIT ?""",
                (user_id, ticker.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM recommendations
                   WHERE user_id = ?
                   ORDER BY generated_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="No recommendation history found")
    return [_rec_from_row(r) for r in rows]


def _rec_from_row(row) -> RecommendationResponse:
    return RecommendationResponse(
        id=row["id"],
        user_id=row["user_id"],
        ticker=row["ticker"],
        signal=row["signal"],
        combined_score=row["combined_score"],
        price_score=row["price_score"],
        technical_score=row["technical_score"],
        sentiment_score=row["sentiment_score"],
        current_price=row["current_price"],
        reason=row["reason"],
        affordable_shares=row["affordable_shares"],
        unrealized_gain_loss=row["unrealized_gain_loss"],
        generated_at=row["generated_at"],
    )
