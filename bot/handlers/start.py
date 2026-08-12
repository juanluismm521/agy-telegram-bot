"""
/start and /help command handlers.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = """
🤖 **AGY Telegram Bot**

Hey! I'm your bridge to **Antigravity** (AGY).
Send me a message and I'll forward it to AGY.

**Available commands:**

💬 **Chat**
• Send a text message → I'll forward it to AGY
• `/new` — New conversation (reset context)

🤖 **Models**
• `/model` — View current model
• `/models` — List available models
• `/model <name>` — Switch model

📊 **Stats**
• `/quota` — Usage statistics
• `/usage` — AGY plan limits (5h / weekly)
• `/status` — Bot and session status

🔧 **Management**
• `/history` — Recent messages from the session
• `/clear` — Clear all history
• `/restart` — Restart the AGY session

💡 _Powered by Google Antigravity SDK_
"""

HELP_MESSAGE = WELCOME_MESSAGE


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command."""
    user = update.effective_user
    logger.info(f"/start from {user.full_name} (id={user.id})")

    # Upsert user in database
    db = context.bot_data.get("db")
    if db:
        await db.upsert_user(user.id, user.username or "")

    await update.message.reply_text(
        WELCOME_MESSAGE,
        parse_mode="Markdown",
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command."""
    await update.message.reply_text(
        HELP_MESSAGE,
        parse_mode="Markdown",
    )
