import sqlite3
from datetime import datetime

from app.database import get_db
from app.finnhub_service import get_quote


PRICE_WEIGHT = 0.5
SENTIMENT_WEIGHT = 0.5

STRONG_BUY_THRESHOLD = 0.3
BUY_THRESHOLD = 0.1
SELL_THRESHOLD = -0.1
STRONG_SELL_THRESHOLD = -0.3


def _compute_price_score(ticker: str, conn: sqlite3.Connection) -> tuple[float, str]:
    try:
        quote = get_quote(ticker)
    except Exception:
        quote = {}

    if not quote or not quote.get("current_price"):
        snapshot = conn.execute(
            """SELECT current_price, percent_change FROM price_snapshots
               WHERE ticker = ? ORDER BY fetched_at DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
        if snapshot:
            pct = snapshot["percent_change"] or 0.0
            score = max(-1.0, min(1.0, pct / 5.0))
            return score, f"Price from snapshot: ${snapshot['current_price']:.2f} ({pct:+.2f}%)"
        return 0.0, "No price data available"

    pct = quote.get("percent_change", 0.0) or 0.0
    score = max(-1.0, min(1.0, pct / 5.0))

    with get_db() as db:
        db.execute(
            """INSERT INTO price_snapshots
               (ticker, current_price, change, percent_change, high, low, open, previous_close)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote["ticker"],
                quote["current_price"],
                quote.get("change"),
                quote.get("percent_change"),
                quote.get("high"),
                quote.get("low"),
                quote.get("open"),
                quote.get("previous_close"),
            ),
        )

    return score, f"Current: ${quote['current_price']:.2f} ({pct:+.2f}%)"


def _compute_sentiment_score(ticker: str, conn: sqlite3.Connection) -> tuple[float, str]:
    rows = conn.execute(
        """SELECT sentiment_compound, sentiment_label FROM news_articles
           WHERE ticker = ? ORDER BY analyzed_at DESC LIMIT 20""",
        (ticker,),
    ).fetchall()
    if not rows:
        return 0.0, "No sentiment data"

    compounds = [r["sentiment_compound"] for r in rows if r["sentiment_compound"] is not None]
    if not compounds:
        return 0.0, "No sentiment scores"

    avg = sum(compounds) / len(compounds)
    bullish = sum(1 for r in rows if r["sentiment_label"] == "bullish")
    bearish = sum(1 for r in rows if r["sentiment_label"] == "bearish")
    score = max(-1.0, min(1.0, avg * 2))
    return score, f"Avg sentiment: {avg:.3f} ({bullish}B/{bearish}S from {len(rows)} articles)"


def _signal_from_score(score: float) -> str:
    if score >= STRONG_BUY_THRESHOLD:
        return "STRONG_BUY"
    if score >= BUY_THRESHOLD:
        return "BUY"
    if score <= STRONG_SELL_THRESHOLD:
        return "STRONG_SELL"
    if score <= SELL_THRESHOLD:
        return "SELL"
    return "HOLD"


def _get_current_price(ticker: str, conn: sqlite3.Connection) -> float:
    try:
        quote = get_quote(ticker)
        if quote and quote.get("current_price"):
            return quote["current_price"]
    except Exception:
        pass
    snapshot = conn.execute(
        "SELECT current_price FROM price_snapshots WHERE ticker = ? ORDER BY fetched_at DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if snapshot:
        return snapshot["current_price"]
    return 0.0


def generate_recommendations(user_id: int) -> list[dict]:
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return []

        holdings = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
        watchlist = conn.execute(
            "SELECT DISTINCT ticker FROM watchlist WHERE user_id = ?", (user_id,)
        ).fetchall()

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

        holding_map: dict[str, dict] = {}
        for h in holdings:
            holding_map[h["ticker"]] = {
                "shares": h["shares"],
                "avg_cost_basis": h["avg_cost_basis"],
            }

        all_tickers = sorted(
            set(h["ticker"] for h in holdings) | set(r["ticker"] for r in watchlist)
        )

        results = []
        for ticker in all_tickers:
            price_score, price_reason = _compute_price_score(ticker, conn)
            sent_score, sent_reason = _compute_sentiment_score(ticker, conn)

            combined = price_score * PRICE_WEIGHT + sent_score * SENTIMENT_WEIGHT
            signal = _signal_from_score(combined)

            current_price = _get_current_price(ticker, conn)

            affordable_shares = None
            unrealized_gl = None
            reasons = [price_reason, sent_reason]

            if ticker in holding_map:
                h = holding_map[ticker]
                if current_price > 0:
                    cost_basis = h["shares"] * h["avg_cost_basis"]
                    market_val = h["shares"] * current_price
                    unrealized_gl = round(market_val - cost_basis, 2)
                if signal in ("SELL", "STRONG_SELL"):
                    reasons.append(f"Holding {h['shares']} shares (P&L: ${unrealized_gl or 0:.2f})")
            else:
                reasons.append("Not currently held (watchlist)")

            if signal in ("BUY", "STRONG_BUY") and current_price > 0:
                affordable_shares = round(remaining_budget / current_price, 4)
                if affordable_shares > 0:
                    reasons.append(f"Can afford {affordable_shares:.2f} shares with ${remaining_budget:.2f} budget")
                else:
                    reasons.append(f"Insufficient budget (${remaining_budget:.2f})")

            rec = {
                "user_id": user_id,
                "ticker": ticker,
                "signal": signal,
                "combined_score": round(combined, 4),
                "price_score": round(price_score, 4),
                "sentiment_score": round(sent_score, 4),
                "current_price": current_price if current_price > 0 else None,
                "reason": " | ".join(reasons),
                "affordable_shares": affordable_shares,
                "unrealized_gain_loss": unrealized_gl,
            }
            results.append(rec)

        results.sort(key=lambda r: abs(r["combined_score"]), reverse=True)

        for rec in results:
            conn.execute(
                """INSERT INTO recommendations
                   (user_id, ticker, signal, combined_score, price_score, sentiment_score,
                    current_price, reason, affordable_shares, unrealized_gain_loss)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec["user_id"],
                    rec["ticker"],
                    rec["signal"],
                    rec["combined_score"],
                    rec["price_score"],
                    rec["sentiment_score"],
                    rec["current_price"],
                    rec["reason"],
                    rec["affordable_shares"],
                    rec["unrealized_gain_loss"],
                ),
            )

    return results
