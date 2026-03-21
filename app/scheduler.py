import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import get_db
from app.email_service import format_morning_email, is_configured, send_email
from app.recommendation_engine import generate_recommendations

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_scheduler = AsyncIOScheduler(timezone=ET)


async def _morning_report():
    """Generate recommendations for all onboarded users and email any buy signals."""
    logger.info("Running morning report job")

    with get_db() as conn:
        users = conn.execute(
            "SELECT id FROM users WHERE is_onboarded = 1"
        ).fetchall()

    if not users:
        logger.info("No onboarded users — skipping morning report")
        return

    if not is_configured():
        logger.warning("Email not configured — skipping morning report send")
        return

    for user in users:
        user_id = user["id"]
        try:
            recs = generate_recommendations(user_id)
            subject, html = format_morning_email(recs)
            send_email(subject, html)

            with get_db() as conn:
                conn.execute(
                    """INSERT INTO message_log
                       (user_id, message_type, status, body)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, "morning_email", "sent", subject),
                )
            logger.info(f"User {user_id}: morning email sent ({len(actionable)} signals)")
        except Exception as e:
            logger.error(f"User {user_id}: morning report failed — {e}")
            try:
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO message_log
                           (user_id, message_type, status, error)
                           VALUES (?, ?, ?, ?)""",
                        (user_id, "morning_email", "failed", str(e)),
                    )
            except Exception:
                pass


def start_scheduler() -> None:
    _scheduler.add_job(
        _morning_report,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30),
        id="morning_report",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started — morning report runs at 8:30 AM ET on weekdays")


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
