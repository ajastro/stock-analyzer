from fastapi import APIRouter, HTTPException

from app.database import get_db
from app.recommendation_engine import generate_recommendations
from app.schemas import (
    BulkSendResponse,
    MessageLogResponse,
    SendMessageResponse,
)
from app.whatsapp_service import (
    format_recommendations_message,
    send_whatsapp_message,
)

router = APIRouter(prefix="/messaging", tags=["messaging"])


def _send_recommendations_to_user(user_id: int) -> SendMessageResponse:
    with get_db() as conn:
        user = conn.execute(
            "SELECT id, name, phone_number FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        holdings = conn.execute(
            "SELECT id FROM holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
        watchlist = conn.execute(
            "SELECT id FROM watchlist WHERE user_id = ?", (user_id,)
        ).fetchall()
        if not holdings and not watchlist:
            raise HTTPException(
                status_code=400,
                detail="User has no holdings or watchlist items",
            )

    recs = generate_recommendations(user_id)
    if not recs:
        raise HTTPException(
            status_code=400,
            detail="No recommendations generated. Add holdings or watchlist items first.",
        )

    with get_db() as conn:
        from datetime import datetime

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

    body = format_recommendations_message(user["name"], recs, remaining_budget)

    try:
        result = send_whatsapp_message(user["phone_number"], body)
        with get_db() as conn:
            conn.execute(
                """INSERT INTO message_log
                   (user_id, message_sid, message_type, status, body)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, result["sid"], "recommendations", result["status"], body),
            )
        return SendMessageResponse(
            user_id=user_id,
            message_sid=result["sid"],
            status=result["status"],
            message_type="recommendations",
        )
    except RuntimeError as e:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO message_log
                   (user_id, message_sid, message_type, status, body, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, None, "recommendations", "failed", body, str(e)),
            )
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO message_log
                   (user_id, message_sid, message_type, status, body, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, None, "recommendations", "failed", body, str(e)),
            )
        raise HTTPException(
            status_code=502, detail=f"Failed to send WhatsApp message: {e}"
        )


@router.post(
    "/send/{user_id}",
    response_model=SendMessageResponse,
)
def send_recommendations(user_id: int):
    return _send_recommendations_to_user(user_id)


@router.post(
    "/send-all",
    response_model=BulkSendResponse,
)
def send_recommendations_to_all():
    with get_db() as conn:
        users = conn.execute(
            "SELECT id FROM users WHERE is_onboarded = 1"
        ).fetchall()

    if not users:
        raise HTTPException(status_code=400, detail="No onboarded users found")

    results: list[SendMessageResponse] = []
    sent = 0
    failed = 0

    for u in users:
        try:
            result = _send_recommendations_to_user(u["id"])
            results.append(result)
            sent += 1
        except HTTPException:
            results.append(
                SendMessageResponse(
                    user_id=u["id"],
                    message_sid=None,
                    status="failed",
                    message_type="recommendations",
                )
            )
            failed += 1

    return BulkSendResponse(sent=sent, failed=failed, results=results)


@router.get(
    "/log/{user_id}",
    response_model=list[MessageLogResponse],
)
def get_message_log(user_id: int, limit: int = 20):
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        rows = conn.execute(
            """SELECT * FROM message_log
               WHERE user_id = ?
               ORDER BY sent_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()

    return [
        MessageLogResponse(
            id=r["id"],
            user_id=r["user_id"],
            message_sid=r["message_sid"],
            message_type=r["message_type"],
            status=r["status"],
            body=r["body"],
            error=r["error"],
            sent_at=r["sent_at"],
        )
        for r in rows
    ]


@router.get("/status")
def messaging_status():
    import os

    has_sid = bool(os.environ.get("TWILIO_ACCOUNT_SID"))
    has_token = bool(os.environ.get("TWILIO_AUTH_TOKEN"))
    has_from = bool(os.environ.get("TWILIO_WHATSAPP_FROM"))

    configured = has_sid and has_token and has_from

    return {
        "configured": configured,
        "twilio_account_sid": "set" if has_sid else "missing",
        "twilio_auth_token": "set" if has_token else "missing",
        "twilio_whatsapp_from": "set" if has_from else "missing",
    }
