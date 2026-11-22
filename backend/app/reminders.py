import asyncio
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from . import database
from .config import settings

logger = logging.getLogger(__name__)
REMINDER_OFFSETS = {
    "at_due": timedelta(0),
    "15_minutes": timedelta(minutes=15),
    "1_hour": timedelta(hours=1),
    "1_day": timedelta(days=1),
}
MAX_ATTEMPTS = 5


def schedule_task(connection, task: dict[str, Any]) -> None:
    preference = task.get("reminder_preference") or "none"
    due_at = task.get("due_at")
    if preference == "none" or not due_at:
        database.schedule_task_reminder(connection, task["id"], None, "none")
        return
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("The task deadline is not a valid ISO 8601 timestamp.") from exc
    if due.tzinfo is None:
        raise ValueError("The task deadline must include a timezone.")
    scheduled = due.astimezone(timezone.utc) - REMINDER_OFFSETS[preference]
    database.schedule_task_reminder(
        connection,
        task["id"],
        scheduled.isoformat(),
        preference,
    )


def _send_email(recipient: str, subject: str, body: str) -> None:
    if not settings.smtp_configured:
        raise RuntimeError("SMTP is not fully configured.")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_use_tls:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)


async def send_test_email(recipient: str) -> None:
    if not settings.smtp_configured:
        raise RuntimeError("Configure SMTP_HOST, SMTP_FROM, and matching SMTP credentials in backend/.env.")
    await asyncio.to_thread(
        _send_email,
        recipient,
        "R Matrix task reminder test",
        "This is a test email from R Matrix Personal AI Command Center.",
    )


def _recover_interrupted_jobs() -> None:
    with database.session() as connection:
        connection.execute(
            """UPDATE task_reminders SET status = 'failed',
               last_error = 'Delivery outcome unknown after backend restart; not retried to prevent duplicate email.'
               WHERE status = 'sending'"""
        )
        connection.commit()


def _claim_due_job() -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with database.session() as connection:
        row = connection.execute(
            """SELECT r.id, r.task_id, r.attempt_count, t.title, t.description, t.due_at
               FROM task_reminders r JOIN application_tasks t ON t.id = r.task_id
               WHERE r.status = 'pending' AND r.scheduled_at <= ?
                 AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?)
                 AND t.status != 'done' AND t.deleted_at IS NULL
               ORDER BY r.scheduled_at LIMIT 1""",
            (now, now),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            """UPDATE task_reminders SET status = 'sending', attempt_count = attempt_count + 1
               WHERE id = ? AND status = 'pending'""",
            (row["id"],),
        )
        connection.commit()
        if cursor.rowcount != 1:
            return None
        return dict(row)


def _recipient() -> str:
    with database.session() as connection:
        return database.get_reminder_recipient(connection)


def _record_result(job_id: str, *, error: Exception | None) -> None:
    with database.session() as connection:
        row = connection.execute(
            "SELECT attempt_count FROM task_reminders WHERE id = ? AND status = 'sending'",
            (job_id,),
        ).fetchone()
        if row is None:
            return
        if error is None:
            connection.execute(
                """UPDATE task_reminders SET status = 'sent', delivered_at = ?, last_error = NULL,
                   next_attempt_at = NULL WHERE id = ? AND status = 'sending'""",
                (database.utc_now(), job_id),
            )
        elif row["attempt_count"] >= MAX_ATTEMPTS:
            connection.execute(
                """UPDATE task_reminders SET status = 'failed', last_error = ?,
                   next_attempt_at = NULL WHERE id = ? AND status = 'sending'""",
                (f"Email delivery failed after {MAX_ATTEMPTS} attempts ({type(error).__name__}).", job_id),
            )
        else:
            retry_at = datetime.now(timezone.utc) + timedelta(
                minutes=min(6 * (2 ** (row["attempt_count"] - 1)), 360)
            )
            connection.execute(
                """UPDATE task_reminders SET status = 'pending', last_error = ?,
                   next_attempt_at = ? WHERE id = ? AND status = 'sending'""",
                (
                    f"Email delivery failed ({type(error).__name__}); retry scheduled.",
                    retry_at.isoformat(),
                    job_id,
                ),
            )
        connection.commit()


async def process_due_reminders() -> None:
    recipient = _recipient()
    if not recipient or not settings.smtp_configured:
        return
    while True:
        job = _claim_due_job()
        if job is None:
            return
        if not job["due_at"]:
            _record_result(job["id"], error=ValueError("Task has no deadline."))
            continue
        due = datetime.fromisoformat(job["due_at"].replace("Z", "+00:00"))
        body = (
            f"Task: {job['title']}\n"
            f"Description: {job['description'] or 'No description'}\n"
            f"Deadline (Asia/Kolkata): {due.astimezone(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M')}\n"
        )
        try:
            await asyncio.to_thread(_send_email, recipient, f"R Matrix task reminder: {job['title']}", body)
        except (OSError, smtplib.SMTPException, ssl.SSLError, RuntimeError) as exc:
            _record_result(job["id"], error=exc)
            logger.warning("Task reminder delivery failed for job %s (%s).", job["id"], type(exc).__name__)
        else:
            _record_result(job["id"], error=None)


async def reminder_worker(stop: asyncio.Event) -> None:
    _recover_interrupted_jobs()
    while not stop.is_set():
        try:
            await process_due_reminders()
        except (OSError, smtplib.SMTPException, ssl.SSLError):
            logger.exception("Reminder worker could not process its queue.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except TimeoutError:
            pass


def reminder_status(connection) -> dict[str, Any]:
    counts = {
        row["status"]: row["count"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM task_reminders GROUP BY status"
        ).fetchall()
    }
    return {
        "recipient_email": database.get_reminder_recipient(connection),
        "smtp_configured": settings.smtp_configured,
        "smtp_host": settings.smtp_host or None,
        "counts": {key: counts.get(key, 0) for key in ("pending", "sent", "failed", "cancelled")},
        "local_scheduler_notice": "The backend and machine must be running to send reminders on time. Eligible pending reminders are processed after restart.",
    }
