from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.email_service import format_morning_email, is_configured, send_email
from app.recommendation_engine import generate_recommendations
from app.schemas import MessageLogResponse
from app.screener import get_cached_results, run_deep_screener
from app.weekly_report import format_weekly_email, generate_weekly_data

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

    holdings_tickers, screener_results = _get_screener_data(user_id)
    _, html = format_morning_email(recs, holdings_tickers, screener_results)
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

    holdings_tickers, screener_results = _get_screener_data(user_id)
    subject, html = format_morning_email(recs, holdings_tickers, screener_results)
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


@router.post("/trigger-morning-report")
async def trigger_morning_report():
    """Manually trigger the morning report job right now (for testing the scheduler logic)."""
    from app.scheduler import _morning_report
    await _morning_report()
    return {"status": "ok", "message": "Morning report job ran — check your inbox and /messaging/log."}


@router.get("/preview-weekly/{user_id}", response_class=HTMLResponse)
def preview_weekly(user_id: int):
    """Preview the weekly report email in the browser without sending it."""
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

    data = generate_weekly_data(user_id)
    if not data:
        return HTMLResponse("<h2>No data available — add holdings and run analysis first.</h2>")

    _, html = format_weekly_email(data)
    return HTMLResponse(html)


def _get_screener_data(user_id: int) -> tuple[set[str], list[dict]]:
    """Returns (holdings_tickers, screener_results) for a user."""
    with get_db() as conn:
        holdings_tickers = {
            r["ticker"] for r in conn.execute(
                "SELECT DISTINCT ticker FROM holdings WHERE user_id = ?", (user_id,)
            ).fetchall()
        }
        watchlist_tickers = {
            r["ticker"] for r in conn.execute(
                "SELECT DISTINCT ticker FROM watchlist WHERE user_id = ?", (user_id,)
            ).fetchall()
        }
    exclude = holdings_tickers | watchlist_tickers
    screener_results = get_cached_results(exclude_tickers=exclude)
    return holdings_tickers, screener_results


@router.post("/warm-screener")
async def warm_screener_endpoint():
    """
    Manually trigger the deep screener (normally runs at 6 AM CT via scheduler).
    Runs the full 5-component analysis on all S&P 500 stocks and stores results in DB.
    Takes ~7 minutes to complete.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_deep_screener)
    return {"status": "ok", "message": "Deep screener complete — results stored in DB and ready for the next email."}


@router.post("/trigger-weekly-report")
async def trigger_weekly_report():
    """Manually trigger the weekly report job right now (for testing)."""
    from app.scheduler import _weekly_report
    await _weekly_report()
    return {"status": "ok", "message": "Weekly report job ran — check your inbox and /messaging/log."}
