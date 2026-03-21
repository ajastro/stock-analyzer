from fastapi import APIRouter, HTTPException

from app.database import get_db
from app.email_service import is_configured, send_email
from app.schemas import MessageLogResponse

router = APIRouter(prefix="/messaging", tags=["messaging"])


@router.get("/log/{user_id}", response_model=list[MessageLogResponse])
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
    return {
        "email_configured": is_configured(),
        "delivery": "email",
        "schedule": "8:30 AM ET, weekdays",
    }


@router.post("/test-email")
def test_email():
    """Send a test email to verify SMTP configuration."""
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Email not configured. Set SMTP_USER, SMTP_PASSWORD, and ALERT_EMAIL in env vars.",
        )
    try:
        send_email(
            subject="Stock Analyzer — Test Email",
            html_body="<h2>✅ Email is working!</h2><p>Your Stock Analyzer email configuration is set up correctly.</p>",
        )
        return {"status": "sent", "message": "Test email sent successfully — check your inbox."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email failed: {e}")
