"""
/goal — set a standing objective that keeps going even across a quota wait.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def goal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /goal <text> — set a goal. /goal — show it. /goal off — clear it.
    """
    user = update.effective_user
    user_id = user.id
    db = context.bot_data.get("db")
    args = context.args

    if not db:
        await update.message.reply_text("❌ Database not available.")
        return

    await db.upsert_user(user_id, user.username or "")

    if not args:
        goal = await db.get_goal(user_id)
        if not goal:
            await update.message.reply_text(
                "ℹ️ No tienes ningún objetivo activo.\n"
                "Usa `/goal <descripción>` para fijar uno: si el bot se queda sin cuota a mitad de una "
                "tarea, seguirá solo en cuanto se restablezca, sin que tengas que escribir nada.\n"
                "`/goal off` lo desactiva.",
                parse_mode="Markdown",
            )
            return
        pending = await db.get_pending_resume(user_id)
        status = (
            "\n\n⏳ Ahora mismo está esperando a que se restablezca la cuota para continuar solo."
            if pending
            else ""
        )
        await update.message.reply_text(f"🎯 Objetivo activo:\n{goal}{status}")
        return

    if args[0].lower() in ("off", "clear", "stop"):
        had_goal = await db.get_goal(user_id)
        await db.clear_goal(user_id)
        await update.message.reply_text(
            "🎯 Objetivo desactivado." if had_goal else "ℹ️ No tenías ningún objetivo activo."
        )
        return

    goal_text = " ".join(args)
    await db.set_goal(user_id, goal_text)
    await update.message.reply_text(
        f"🎯 Objetivo fijado:\n{goal_text}\n\n"
        "Si el bot se queda sin cuota a mitad de una tarea, seguirá automáticamente en cuanto se restablezca."
    )
    logger.info(f"User {user_id} set a goal: {goal_text[:100]}")
