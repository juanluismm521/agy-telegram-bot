"""
Usage handler — AGY plan limits (the data behind AGY's own /usage command).
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.usage import get_usage_message

logger = logging.getLogger(__name__)


async def usage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /usage — Show AGY plan limits (5h and weekly windows, per model family).
    """
    message = await update.message.reply_text("📊 Checking AGY plan limits…")
    await message.edit_text(await get_usage_message(), parse_mode="Markdown")
