import uuid

from fastapi import APIRouter, HTTPException
from app.database import get_db
from app.schemas import (
    UserCreate,
    UserResponse,
    OnboardRequest,
    HoldingResponse,
)

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/register", response_model=UserResponse, status_code=201)
def register_user(user: UserCreate):
    phone = user.phone_number or f"auto_{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (name, phone_number) VALUES (?, ?)",
            (user.name, phone),
        )
        user_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _user_from_row(row)


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_from_row(row)


@router.post("/{user_id}/onboard", response_model=UserResponse)
def onboard_user(user_id: int, req: OnboardRequest):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if row["is_onboarded"]:
            raise HTTPException(
                status_code=400, detail="User is already onboarded"
            )

        conn.execute(
            "UPDATE users SET is_onboarded = 1, has_prior_stocks = ? WHERE id = ?",
            (1 if req.has_prior_stocks else 0, user_id),
        )

        if req.has_prior_stocks and req.holdings:
            for h in req.holdings:
                conn.execute(
                    """INSERT INTO holdings
                       (user_id, ticker, shares, avg_cost_basis, acquired_date)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, h.ticker.upper(), h.shares, h.avg_cost_basis, h.acquired_date),
                )

        from datetime import date as date_mod

        today = date_mod.today().isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO daily_budgets (user_id, date, base_budget)
               VALUES (?, ?, 100.0)""",
            (user_id, today),
        )

        updated = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _user_from_row(updated)


@router.get("/{user_id}/portfolio", response_model=list[HoldingResponse])
def get_portfolio(user_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        rows = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        ).fetchall()
    return [_holding_from_row(r) for r in rows]


def _user_from_row(row: dict) -> UserResponse:
    return UserResponse(
        id=row["id"],
        name=row["name"],
        phone_number=row["phone_number"],
        is_onboarded=bool(row["is_onboarded"]),
        has_prior_stocks=bool(row["has_prior_stocks"]),
        created_at=row["created_at"],
    )


def _holding_from_row(row: dict) -> HoldingResponse:
    return HoldingResponse(
        id=row["id"],
        user_id=row["user_id"],
        ticker=row["ticker"],
        shares=row["shares"],
        avg_cost_basis=row["avg_cost_basis"],
        acquired_date=row["acquired_date"],
        created_at=row["created_at"],
    )
