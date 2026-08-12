"""
Quota/stats handler — show usage statistics.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.utils.formatting import format_usage_stats, format_session_info

logger = logging.getLogger(__name__)


async def quota_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /quota — Show usage statistics from DB + current session info.
    """
    user_id = update.effective_user.id
    db = context.bot_data.get("db")
    agent_manager = context.bot_data["agent_manager"]

    parts = []

    # Current session info
    session_info = agent_manager.get_session_info(user_id)
    if session_info:
        parts.append("**🔵 Active Session**\n")
        parts.append(f"🤖 Model: `{session_info['model']}`")
        parts.append(f"💬 Messages (session): {session_info['message_count']}")
        parts.append(f"⏱ Uptime: {session_info['uptime_minutes']} min")

        if session_info.get("total_input_tokens") or session_info.get("total_output_tokens"):
            parts.append(f"📥 Tokens in: {session_info['total_input_tokens']:,}")
            parts.append(f"📤 Tokens out: {session_info['total_output_tokens']:,}")
            total = session_info["total_input_tokens"] + session_info["total_output_tokens"]
            parts.append(f"📊 Total tokens (session): {total:,}")

        parts.append("")
    else:
        parts.append("⚪ No active session\n")

    # DB stats
    if db:
        stats = await db.get_usage_stats(user_id)
        parts.append("**📊 Overall Statistics**\n")
        parts.append(f"💬 Today: {stats.get('messages_24h', 0)} messages")
        parts.append(f"📅 This week: {stats.get('messages_7d', 0)} messages")
        parts.append(f"📆 This month: {stats.get('messages_30d', 0)} messages")
        parts.append(f"📝 Total: {stats.get('total_messages', 0)} messages")
        parts.append(f"🗂 Conversations: {stats.get('total_conversations', 0)}")

        if stats.get("total_tokens", 0) > 0:
            parts.append(f"🔢 Cumulative tokens: {stats['total_tokens']:,}")

        if stats.get("models"):
            parts.append("\n**🧠 Usage per Model**")
            # Sort by messages count
            for model_name, data in sorted(stats["models"].items(), key=lambda x: x[1]["messages"], reverse=True):
                parts.append(f"🔹 `{model_name}`: {data['messages']} msgs")

    text = "\n".join(parts)
    await update.message.reply_text(text, parse_mode="Markdown")
