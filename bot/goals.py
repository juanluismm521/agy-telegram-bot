"""
Goal-directed auto-resume.

/goal lets a user set a standing objective. When a quota/rate-limit error interrupts
a turn while a goal is active, the bot still shows the usual "you need to wait"
message (unchanged), but also schedules a background retry: once the AGY CLI's own
reported reset time passes, it resubmits the message on the same project (with the
goal as a reminder) and delivers the result on its own — no need for the user to
nudge it again.
"""
import asyncio
import logging
import re
import time

from bot.utils.formatting import split_message

logger = logging.getLogger(__name__)

QUOTA_KEYWORDS = (
    "quota reached",
    "usage limit",
    "rate limit",
    "limit reached",
    "resets in",
    "upgrade your subscription",
    "upgrade your plan",
    "try again later",
)

# AGY's own quota error already tells us exactly how long is left, e.g.:
# "Individual quota reached. Please upgrade your subscription to increase your
# limits. Resets in 21h24m36s."
_RESET_IN_RE = re.compile(r"resets?\s+in\s+(?:(\d+)\s*h)?(?:(\d+)\s*m)?(?:(\d+)\s*s)?", re.IGNORECASE)

DEFAULT_RETRY_SECONDS = 15 * 60  # fallback poll interval when no duration could be parsed
MAX_WAIT_SECONDS = 30 * 3600  # give up retrying after ~30h
RESUME_BUFFER_SECONDS = 45  # small safety margin past the reported reset time


def is_quota_error(text: str) -> bool:
    low = (text or "").lower()
    return any(kw in low for kw in QUOTA_KEYWORDS)


def parse_reset_delay(text: str) -> float | None:
    """Best-effort parse of "Resets in 21h24m36s" style text -> seconds."""
    m = _RESET_IN_RE.search(text or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + s)


def _resume_prompt(goal_text: str, original_message: str) -> str:
    return (
        f"[Auto-resumed: quota had run out and has now reset. Active goal: {goal_text}]\n\n"
        f"{original_message}"
    )


async def schedule_resume(
    application, user_id: int, chat_id: int, error_text: str, original_message: str
) -> str | None:
    """If the user has an active goal, persist + schedule an auto-retry.

    Returns a note to append to the error message shown to the user, or None if
    there is no active goal (in which case behavior is unchanged).
    """
    db = application.bot_data["db"]
    goal_text = await db.get_goal(user_id)
    if not goal_text:
        return None

    if await db.get_pending_resume(user_id):
        return "🎯 Ya tengo tu objetivo en cola — seguiré en cuanto se restablezca la cuota."

    delay = parse_reset_delay(error_text)
    resume_at = time.time() + (delay if delay is not None else DEFAULT_RETRY_SECONDS) + RESUME_BUFFER_SECONDS
    await db.set_pending_resume(user_id, original_message, resume_at)

    application.create_task(
        _resume_worker(application, user_id, chat_id, original_message),
        name=f"goal-resume-{user_id}",
    )
    return "🎯 Tienes un objetivo activo — seguiré automáticamente en cuanto se restablezca la cuota, sin que hagas nada."


async def resume_pending_on_startup(application):
    """Reschedule any auto-resume that was still pending when the bot last stopped."""
    db = application.bot_data["db"]
    rows = await db.get_all_pending_resumes()
    for row in rows:
        application.create_task(
            _resume_worker(application, row["telegram_id"], row["telegram_id"], row["pending_message"]),
            name=f"goal-resume-{row['telegram_id']}",
        )
    if rows:
        logger.info("Rescheduled %d pending goal auto-resume(s) after restart", len(rows))


async def _resume_worker(application, user_id: int, chat_id: int, original_message: str):
    db = application.bot_data["db"]
    bot = application.bot
    agent_manager = application.bot_data["agent_manager"]

    waited = 0.0
    while True:
        pending = await db.get_pending_resume(user_id)
        goal_text = await db.get_goal(user_id)
        if not pending or not goal_text:
            return

        delay = max(0.0, pending["pending_resume_at"] - time.time())
        await asyncio.sleep(min(delay, MAX_WAIT_SECONDS - waited) if delay > 0 else 0)
        waited += delay

        result = await _try_resume(agent_manager, db, bot, chat_id, user_id, goal_text, original_message)
        if result == "ok":
            await db.clear_pending_resume(user_id)
            return
        if waited >= MAX_WAIT_SECONDS:
            try:
                await bot.send_message(
                    chat_id,
                    "⚠️ Llevo horas esperando y la cuota sigue agotada — dejo de reintentar tu objetivo "
                    "automáticamente. Envía un mensaje cuando quieras retomarlo.",
                )
            except Exception:  # noqa: BLE001
                pass
            await db.clear_pending_resume(user_id)
            return

        # Still exhausted (or errored) — push the retry point back and loop.
        await db.set_pending_resume(user_id, original_message, time.time() + DEFAULT_RETRY_SECONDS)


async def _try_resume(agent_manager, db, bot, chat_id, user_id, goal_text, original_message) -> str:
    """Attempt one resume. Returns 'ok', 'quota', or 'error'."""
    try:
        await bot.send_message(chat_id, "🎯 Cuota restablecida — retomando tu objetivo automáticamente…")
    except Exception:  # noqa: BLE001
        pass

    prompt = _resume_prompt(goal_text, original_message)
    full_text = ""
    try:
        async for chunk in agent_manager.chat_stream(user_id, prompt):
            full_text += chunk
    except Exception:  # noqa: BLE001
        logger.exception("goal auto-resume failed for user %s", user_id)
        return "error"

    if is_quota_error(full_text):
        return "quota"

    if not full_text.strip():
        full_text = "✅ Done (no text output from AGY)"

    conv = await db.get_active_conversation(user_id)
    if conv:
        await db.add_message(conv["id"], "assistant", full_text)

    for chunk in split_message(full_text):
        try:
            await bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id, chunk)
    return "ok"
