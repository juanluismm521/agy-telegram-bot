"""
Admin handlers — bot status, session restart.
"""

import logging
import time
import os
import re

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status — Show bot health, active sessions, and uptime.
    """
    agent_manager = context.bot_data["agent_manager"]
    start_time = context.bot_data.get("start_time", time.time())

    uptime_seconds = time.time() - start_time
    uptime_hours = uptime_seconds / 3600
    uptime_minutes = (uptime_seconds % 3600) / 60

    all_sessions = agent_manager.get_all_sessions_info()

    lines = [
        "🟢 **Bot Status**\n",
        f"⏱ Uptime: {int(uptime_hours)}h {int(uptime_minutes)}m",
        f"👥 Active sessions: {all_sessions['active_sessions']}",
    ]

    if all_sessions["sessions"]:
        lines.append("\n**Sessions:**")
        for uid, info in all_sessions["sessions"].items():
            status = "🔄" if info.get("is_busy") else "✅"
            lines.append(
                f"  {status} User `{uid}` — `{info['model']}` "
                f"({info['message_count']} msgs, idle {info['idle_minutes']}m)"
            )

    lines.append(f"\n🔧 Version: 1.0.0")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /restart — Restart the current AGY session (same model, fresh context).
    """
    user_id = update.effective_user.id
    agent_manager = context.bot_data["agent_manager"]
    db = context.bot_data.get("db")

    # End current conversation in DB
    if db:
        conv = await db.get_active_conversation(user_id)
        if conv:
            await db.end_conversation(conv["id"])

    # Restart session
    result = await agent_manager.new_session(user_id)

    # Create new conversation in DB
    if db:
        session_info = agent_manager.get_session_info(user_id)
        model = session_info["model"] if session_info else ""
        await db.create_conversation(user_id, model)

    await update.message.reply_text(
        f"🔄 Session restarted!\n{result}",
        parse_mode="Markdown",
    )
    logger.info(f"User {user_id} restarted their session")

async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /timeout <minutes> — Update the session timeout in minutes.
    """
    if not context.args:
        await update.message.reply_text("ℹ️ Usage: `/timeout <minutes>`", parse_mode="Markdown")
        return

    try:
        minutes = int(context.args[0])
        if minutes <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid positive number for minutes.")
        return

    # Update runtime config
    agent_manager = context.bot_data["agent_manager"]
    agent_manager._session_timeout = minutes * 60
    
    settings = context.bot_data["settings"]
    settings.session_timeout_minutes = minutes

    # Update .env file
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            content = f.read()
        
        if "SESSION_TIMEOUT_MINUTES=" in content:
            content = re.sub(r"SESSION_TIMEOUT_MINUTES=.*", f"SESSION_TIMEOUT_MINUTES={minutes}", content)
        else:
            content += f"\nSESSION_TIMEOUT_MINUTES={minutes}\n"
            
        with open(env_path, "w") as f:
            f.write(content)

    await update.message.reply_text(f"✅ Session timeout updated to **{minutes} minutes**.", parse_mode="Markdown")
    logger.info(f"Session timeout updated to {minutes} minutes by {update.effective_user.id}")
