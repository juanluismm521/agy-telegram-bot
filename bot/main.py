"""
AGY Telegram Bot — Main entry point.

Assembles all components and starts the bot with polling.
"""

import logging
import time
import sys

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import load_settings
from bot.agent import AGYSessionManager
from bot.database.db import Database
from bot.middleware.auth import require_auth, require_auth_callback
from bot.handlers.start import start_handler, help_handler
from bot.handlers.chat import chat_handler
from bot.handlers.models import models_handler, model_handler, model_callback_handler
from bot.handlers.session import new_handler, history_handler, clear_handler
from bot.handlers.quota import quota_handler
from bot.handlers.usage import usage_handler
from bot.handlers.goal import goal_handler
from bot.goals import resume_pending_on_startup
from bot.utils.timezone import warm_timezone
from bot.handlers.admin import status_handler, restart_handler, timeout_handler

logger = logging.getLogger(__name__)


def create_app():
    """Create and configure the Telegram bot application."""

    # Load settings
    settings = load_settings()

    # Configure logging
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=settings.log_level_int,
    )

    # The HTTP stack logs one record per getUpdates poll — every few seconds, forever,
    # with the bot token embedded in the request URL. Keep it out of the log file.
    for noisy in ("httpx", "httpcore", "telegram.ext.ExtBot", "telegram.Bot"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("🚀 Starting AGY Telegram Bot...")
    logger.info(f"   Allowed users: {settings.allowed_user_ids}")
    logger.info(f"   Default model: {settings.default_model or '(AGY default)'}")
    logger.info(f"   Streaming: {settings.enable_streaming}")
    logger.info(f"   Session timeout: {settings.session_timeout_minutes}min")

    # Build the application
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Store shared dependencies in bot_data
    app.bot_data["settings"] = settings
    app.bot_data["start_time"] = time.time()

    # Auth wrapper
    auth = require_auth(settings.allowed_user_ids)
    auth_cb = require_auth_callback(settings.allowed_user_ids)

    # Register command handlers (with auth)
    app.add_handler(CommandHandler("start", auth(start_handler)))
    app.add_handler(CommandHandler("help", auth(help_handler)))
    app.add_handler(CommandHandler("new", auth(new_handler)))
    app.add_handler(CommandHandler("models", auth(models_handler)))
    app.add_handler(CommandHandler("model", auth(model_handler)))
    app.add_handler(CommandHandler("quota", auth(quota_handler)))
    app.add_handler(CommandHandler("usage", auth(usage_handler)))
    app.add_handler(CommandHandler("history", auth(history_handler)))
    app.add_handler(CommandHandler("clear", auth(clear_handler)))
    app.add_handler(CommandHandler("status", auth(status_handler)))
    app.add_handler(CommandHandler("restart", auth(restart_handler)))
    app.add_handler(CommandHandler("timeout", auth(timeout_handler)))
    app.add_handler(CommandHandler("goal", auth(goal_handler)))

    # Pass-through slash commands to AGY
    for cmd in ["plan", "schedule", "learn"]:
        app.add_handler(CommandHandler(cmd, auth(chat_handler)))

    # Callback query handler for inline keyboards (model selection)
    app.add_handler(
        CallbackQueryHandler(auth_cb(model_callback_handler), pattern=r"^model:")
    )

    # Text message handler (chat with AGY) — must be last
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            auth(chat_handler),
        )
    )

    # Lifecycle hooks for startup/shutdown
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    return app


async def _post_init(application):
    """Initialize async components after the app is built."""
    settings = application.bot_data["settings"]

    await warm_timezone()

    # Initialize database
    db = Database(settings.db_path)
    await db.init()
    application.bot_data["db"] = db

    # Initialize AGY session manager
    agent_manager = AGYSessionManager(
        default_model=settings.default_model,
        session_timeout_minutes=settings.session_timeout_minutes,
    )
    await agent_manager.start()
    application.bot_data["agent_manager"] = agent_manager

    await resume_pending_on_startup(application)

    logger.info("✅ AGY Telegram Bot is ready!")

    # Set bot commands for the menu
    from telegram import BotCommand

    commands = [
        BotCommand("start", "Welcome message"),
        BotCommand("help", "Help and commands"),
        BotCommand("new", "New conversation"),
        BotCommand("model", "View/change model"),
        BotCommand("models", "List models"),
        BotCommand("quota", "Usage stats"),
        BotCommand("usage", "AGY plan limits (5h / weekly)"),
        BotCommand("goal", "Set/view a standing goal (auto-resumes after quota reset)"),
        BotCommand("history", "Session history"),
        BotCommand("clear", "Clear history"),
        BotCommand("status", "Bot status"),
        BotCommand("restart", "Restart session"),
        BotCommand("timeout", "Set timeout in minutes"),
    ]
    await application.bot.set_my_commands(commands)


async def _post_shutdown(application):
    """Clean up resources on shutdown."""
    logger.info("🛑 Shutting down AGY Telegram Bot...")

    # Stop agent manager
    agent_manager = application.bot_data.get("agent_manager")
    if agent_manager:
        await agent_manager.stop()

    # Close database
    db = application.bot_data.get("db")
    if db:
        await db.close()

    logger.info("👋 Goodbye!")


def main():
    """Entry point."""
    app = create_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
