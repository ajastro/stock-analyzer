import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import get_db
from app.email_service import format_morning_email, is_configured, send_email
from app.recommendation_engine import generate_recommendations
from app.screener import get_cached_results, run_deep_screener
from app.weekly_report import format_weekly_email, generate_weekly_data

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/Chicago")
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
            subject, html = format_morning_email(recs, holdings_tickers, screener_results)
            send_email(subject, html)

            with get_db() as conn:
                conn.execute(
                    """INSERT INTO message_log
                       (user_id, message_type, status, body)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, "morning_email", "sent", subject),
                )
            logger.info(f"User {user_id}: morning email sent ({len(recs)} recs)")
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


async def _weekly_report():
    """Generate and send the weekly report for all onboarded users."""
    logger.info("Running weekly report job")

    with get_db() as conn:
        users = conn.execute(
            "SELECT id FROM users WHERE is_onboarded = 1"
        ).fetchall()

    if not users:
        logger.info("No onboarded users — skipping weekly report")
        return

    if not is_configured():
        logger.warning("Email not configured — skipping weekly report send")
        return

    for user in users:
        user_id = user["id"]
        try:
            data = generate_weekly_data(user_id)
            if not data:
                continue
            subject, html = format_weekly_email(data)
            send_email(subject, html)

            with get_db() as conn:
                conn.execute(
                    """INSERT INTO message_log (user_id, message_type, status, body)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, "weekly_email", "sent", subject),
                )
            logger.info(f"User {user_id}: weekly report sent")
        except Exception as e:
            logger.error(f"User {user_id}: weekly report failed — {e}")
            try:
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO message_log (user_id, message_type, status, error)
                           VALUES (?, ?, ?, ?)""",
                        (user_id, "weekly_email", "failed", str(e)),
                    )
            except Exception:
                pass


async def _deep_screener_job():
    """Run the full S&P 500 deep screener and store results in DB."""
    import asyncio
    logger.info("Running deep screener job")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_deep_screener)
    logger.info("Deep screener job complete")


def start_scheduler() -> None:
    _scheduler.add_job(
        _deep_screener_job,
        CronTrigger(day_of_week="mon-fri", hour=6, minute=0),
        id="deep_screener",
        replace_existing=True,
    )
    _scheduler.add_job(
        _morning_report,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=30),
        id="morning_report",
        replace_existing=True,
    )
    _scheduler.add_job(
        _weekly_report,
        CronTrigger(day_of_week="sun", hour=11, minute=0),
        id="weekly_report",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started — deep screener 6:00 AM, daily report 7:30 AM CT weekdays, weekly 11:00 AM CT Sundays")


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
