from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.email_service import format_morning_email, is_configured, send_email
from app.recommendation_engine import generate_recommendations
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


@router.get("/preview/{user_id}", response_class=HTMLResponse)
def preview_email(user_id: int):
    """Preview the email that would be sent for a user without actually sending it."""
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

    recs = generate_recommendations(user_id)
    if not recs:
        return HTMLResponse("<h2>No recommendations to preview — add holdings and run analysis first.</h2>")

    _, html = format_morning_email(recs)
    return HTMLResponse(html)


@router.post("/send/{user_id}")
def send_report(user_id: int):
    """Send the latest recommendations email for a user."""
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

    if not is_configured():
        raise HTTPException(status_code=400, detail="Email not configured. Set RESEND_API_KEY and ALERT_EMAIL.")

    recs = generate_recommendations(user_id)
    if not recs:
        raise HTTPException(status_code=400, detail="No recommendations to send. Run analysis first.")

    subject, html = format_morning_email(recs)
    try:
        send_email(subject, html)
        with get_db() as conn:
            conn.execute(
                """INSERT INTO message_log (user_id, message_type, status, body)
                   VALUES (?, ?, ?, ?)""",
                (user_id, "email", "sent", subject),
            )
        return {"status": "sent", "subject": subject}
    except Exception as e:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO message_log (user_id, message_type, status, error)
                   VALUES (?, ?, ?, ?)""",
                (user_id, "email", "failed", str(e)),
            )
        raise HTTPException(status_code=500, detail=f"Email failed: {e}")


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
