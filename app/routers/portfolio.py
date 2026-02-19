from fastapi import APIRouter, HTTPException
from app.database import get_db
from app.schemas import (
    HoldingCreate,
    HoldingUpdate,
    HoldingResponse,
)

router = APIRouter(prefix="/users/{user_id}/portfolio", tags=["portfolio"])


@router.post("", response_model=HoldingResponse, status_code=201)
def add_holding(user_id: int, holding: HoldingCreate):
    with get_db() as conn:
        _require_user(conn, user_id)
        cursor = conn.execute(
            """INSERT INTO holdings
               (user_id, ticker, shares, avg_cost_basis, acquired_date)
               VALUES (?, ?, ?, ?, ?)""",
            (
                user_id,
                holding.ticker.upper(),
                holding.shares,
                holding.avg_cost_basis,
                holding.acquired_date,
            ),
        )
        row = conn.execute(
            "SELECT * FROM holdings WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _holding_from_row(row)


@router.put("/{holding_id}", response_model=HoldingResponse)
def update_holding(user_id: int, holding_id: int, update: HoldingUpdate):
    with get_db() as conn:
        _require_user(conn, user_id)
        row = conn.execute(
            "SELECT * FROM holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Holding not found")

        fields = {}
        if update.ticker is not None:
            fields["ticker"] = update.ticker.upper()
        if update.shares is not None:
            fields["shares"] = update.shares
        if update.avg_cost_basis is not None:
            fields["avg_cost_basis"] = update.avg_cost_basis
        if update.acquired_date is not None:
            fields["acquired_date"] = update.acquired_date

        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [holding_id, user_id]
        conn.execute(
            f"UPDATE holdings SET {set_clause} WHERE id = ? AND user_id = ?",
            values,
        )
        updated = conn.execute(
            "SELECT * FROM holdings WHERE id = ?", (holding_id,)
        ).fetchone()
    return _holding_from_row(updated)


@router.delete("/{holding_id}", status_code=204)
def delete_holding(user_id: int, holding_id: int):
    with get_db() as conn:
        _require_user(conn, user_id)
        row = conn.execute(
            "SELECT id FROM holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Holding not found")
        conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
    return None


def _require_user(conn, user_id: int) -> None:
    row = conn.execute(
        "SELECT id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")


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
