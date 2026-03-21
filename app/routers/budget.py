from datetime import date as date_mod
from fastapi import APIRouter, HTTPException
from app.database import get_db
from app.schemas import (
    BuyRecord,
    SellRecord,
    DailyBudgetResponse,
    TransactionResponse,
)

router = APIRouter(prefix="/users/{user_id}/budget", tags=["budget"])

DAILY_BASE_BUDGET = 100.0


@router.get("", response_model=DailyBudgetResponse)
def get_daily_budget(user_id: int):
    today = date_mod.today().isoformat()
    with get_db() as conn:
        _require_user(conn, user_id)
        row = conn.execute(
            "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
            (user_id, today),
        ).fetchone()
        if not row:
            conn.execute(
                """INSERT INTO daily_budgets (user_id, date, base_budget)
                   VALUES (?, ?, ?)""",
                (user_id, today, DAILY_BASE_BUDGET),
            )
            row = conn.execute(
                "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
                (user_id, today),
            ).fetchone()
    return _budget_from_row(row)


@router.post("/buy", response_model=TransactionResponse, status_code=201)
def record_buy(user_id: int, buy: BuyRecord):
    today = date_mod.today().isoformat()
    total = buy.shares * buy.price_per_share

    with get_db() as conn:
        _require_user(conn, user_id)
        budget_row = _ensure_budget(conn, user_id, today)
        remaining = (
            budget_row["base_budget"]
            + budget_row["sell_profits"]
            - budget_row["amount_spent"]
        )
        if total > remaining:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient budget. Remaining: ${remaining:.2f}, Required: ${total:.2f}",
            )

        conn.execute(
            "UPDATE daily_budgets SET amount_spent = amount_spent + ? WHERE user_id = ? AND date = ?",
            (total, user_id, today),
        )

        existing = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
            (user_id, buy.ticker.upper()),
        ).fetchone()

        # Check if there's a recent BUY/STRONG_BUY recommendation for this ticker
        recent_rec = conn.execute(
            """SELECT signal FROM recommendations
               WHERE user_id = ? AND ticker = ? AND signal IN ('BUY', 'STRONG_BUY')
               ORDER BY generated_at DESC LIMIT 1""",
            (user_id, buy.ticker.upper()),
        ).fetchone()
        was_alerted = 1 if recent_rec else 0

        if existing:
            new_shares = existing["shares"] + buy.shares
            new_cost = (
                (existing["shares"] * existing["avg_cost_basis"])
                + (buy.shares * buy.price_per_share)
            ) / new_shares
            update_alert = (
                "UPDATE holdings SET shares = ?, avg_cost_basis = ?, "
                "alert_triggered = CASE WHEN alert_triggered = 0 THEN ? ELSE alert_triggered END, "
                "alert_date = CASE WHEN alert_triggered = 0 AND ? = 1 THEN ? ELSE alert_date END "
                "WHERE id = ?"
            )
            conn.execute(update_alert, (new_shares, new_cost, was_alerted, was_alerted, today, existing["id"]))
        else:
            conn.execute(
                """INSERT INTO holdings
                   (user_id, ticker, shares, avg_cost_basis, acquired_date, alert_triggered, alert_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, buy.ticker.upper(), buy.shares, buy.price_per_share, today,
                 was_alerted, today if was_alerted else None),
            )

        cursor = conn.execute(
            """INSERT INTO transactions
               (user_id, ticker, action, shares, price_per_share, total_amount)
               VALUES (?, ?, 'BUY', ?, ?, ?)""",
            (user_id, buy.ticker.upper(), buy.shares, buy.price_per_share, total),
        )
        tx = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _tx_from_row(tx)


@router.post("/sell", response_model=TransactionResponse, status_code=201)
def record_sell(user_id: int, sell: SellRecord):
    today = date_mod.today().isoformat()
    total = sell.shares * sell.price_per_share

    with get_db() as conn:
        _require_user(conn, user_id)
        _ensure_budget(conn, user_id, today)

        holding = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
            (user_id, sell.ticker.upper()),
        ).fetchone()
        if not holding:
            raise HTTPException(
                status_code=404,
                detail=f"No holding found for ticker {sell.ticker.upper()}",
            )
        if holding["shares"] < sell.shares:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient shares. Have: {holding['shares']}, Selling: {sell.shares}",
            )

        profit = (sell.price_per_share - holding["avg_cost_basis"]) * sell.shares

        new_shares = holding["shares"] - sell.shares
        if new_shares <= 0:
            conn.execute("DELETE FROM holdings WHERE id = ?", (holding["id"],))
        else:
            conn.execute(
                "UPDATE holdings SET shares = ? WHERE id = ?",
                (new_shares, holding["id"]),
            )

        if profit > 0:
            conn.execute(
                "UPDATE daily_budgets SET sell_profits = sell_profits + ? WHERE user_id = ? AND date = ?",
                (profit, user_id, today),
            )

        cursor = conn.execute(
            """INSERT INTO transactions
               (user_id, ticker, action, shares, price_per_share, total_amount, profit)
               VALUES (?, ?, 'SELL', ?, ?, ?, ?)""",
            (user_id, sell.ticker.upper(), sell.shares, sell.price_per_share, total, profit),
        )
        tx = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _tx_from_row(tx)


@router.get("/transactions", response_model=list[TransactionResponse])
def get_transactions(user_id: int):
    with get_db() as conn:
        _require_user(conn, user_id)
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [_tx_from_row(r) for r in rows]


def _require_user(conn, user_id: int) -> None:
    row = conn.execute(
        "SELECT id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")


def _ensure_budget(conn, user_id: int, today: str):
    row = conn.execute(
        "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
        (user_id, today),
    ).fetchone()
    if not row:
        conn.execute(
            """INSERT INTO daily_budgets (user_id, date, base_budget)
               VALUES (?, ?, ?)""",
            (user_id, today, DAILY_BASE_BUDGET),
        )
        row = conn.execute(
            "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
            (user_id, today),
        ).fetchone()
    return row


def _budget_from_row(row: dict) -> DailyBudgetResponse:
    remaining = row["base_budget"] + row["sell_profits"] - row["amount_spent"]
    return DailyBudgetResponse(
        user_id=row["user_id"],
        date=row["date"],
        base_budget=row["base_budget"],
        sell_profits=row["sell_profits"],
        amount_spent=row["amount_spent"],
        remaining_budget=remaining,
    )


def _tx_from_row(row: dict) -> TransactionResponse:
    return TransactionResponse(
        id=row["id"],
        user_id=row["user_id"],
        ticker=row["ticker"],
        action=row["action"],
        shares=row["shares"],
        price_per_share=row["price_per_share"],
        total_amount=row["total_amount"],
        profit=row["profit"],
        created_at=row["created_at"],
    )
