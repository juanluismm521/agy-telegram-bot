"""
Chat handler — forwards text messages to AGY and returns responses.

Supports both streaming (progressive message editing) and non-streaming modes.
"""

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from telegram import ReactionTypeEmoji

from bot.utils.formatting import split_message

logger = logging.getLogger(__name__)

# Minimum interval between message edits during streaming (Telegram rate limit)
STREAM_EDIT_INTERVAL = 1.5  # seconds
# Minimum chars accumulated before editing the message
STREAM_MIN_DELTA = 80


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle incoming text messages — forward to AGY and reply with response.

    Uses streaming mode if enabled in settings, otherwise waits for full response.
    """
    user = update.effective_user
    message_text = update.message.text

    if not message_text or not message_text.strip():
        return

    logger.info(f"Chat from {user.full_name} (id={user.id}): {message_text[:100]}...")

    # Get dependencies from bot_data
    agent_manager = context.bot_data["agent_manager"]
    db = context.bot_data.get("db")
    settings = context.bot_data["settings"]

    # Upsert user
    if db:
        await db.upsert_user(user.id, user.username or "")

    # Ensure conversation exists in DB
    conversation_id = None
    if db:
        conv = await db.get_active_conversation(user.id)
        if not conv:
            conversation_id = await db.create_conversation(user.id)
        else:
            conversation_id = conv["id"]

        # Save user message
        await db.add_message(conversation_id, "user", message_text)

        # Build prompt from history
        messages = await db.get_messages(conversation_id, limit=30)
        
        is_slash_cmd = any(message_text.startswith(f"/{cmd}") for cmd in ["goal", "plan", "schedule", "learn"])
        
        if is_slash_cmd:
            prompt_text = f"{message_text}\n\n[Context - Conversation history:]\n"
            for msg in messages[:-1]:
                role = "User" if msg["role"] == "user" else "Assistant"
                prompt_text += f"{role}: {msg['content']}\n\n"
        else:
            prompt_text = "Here is the conversation history:\n\n"
            for msg in messages:
                role = "User" if msg["role"] == "user" else "Assistant"
                prompt_text += f"{role}: {msg['content']}\n\n"
            prompt_text += "Please respond to the User's last message."
    else:
        prompt_text = message_text

    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)

    if settings.enable_streaming:
        await _handle_streaming(
            update, context, agent_manager, db, conversation_id, prompt_text, user.id
        )
    else:
        await _handle_blocking(
            update, context, agent_manager, db, conversation_id, prompt_text, user.id
        )


async def _handle_blocking(
    update, context, agent_manager, db, conversation_id, message_text, user_id
):
    """Handle chat in blocking mode — wait for full response then send."""

    # Keep sending typing action while waiting
    typing_task = asyncio.create_task(_keep_typing(update))

    # Set thinking reaction
    try:
        await update.message.set_reaction(reaction=[ReactionTypeEmoji("🤔")])
    except Exception as e:
        logger.debug(f"Could not set reaction: {e}")

    try:
        response_text = await agent_manager.chat(user_id, message_text)
    finally:
        typing_task.cancel()
        try:
            await update.message.set_reaction(reaction=[])
        except Exception:
            pass
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    # Save assistant response
    if db and conversation_id and response_text.strip():
        await db.add_message(conversation_id, "assistant", response_text)

    if not response_text.strip():
        response_text = "✅ Done (No text output from AGY)"

    # Send response (split if too long)
    chunks = split_message(response_text)
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            # Fallback: send without markdown if parsing fails
            await update.message.reply_text(chunk)


async def _handle_streaming(
    update, context, agent_manager, db, conversation_id, message_text, user_id
):
    """Handle chat in streaming mode — progressively edit message with new content."""

    # Set thinking reaction
    try:
        await update.message.set_reaction(reaction=[ReactionTypeEmoji("🤔")])
    except Exception as e:
        logger.debug(f"Could not set reaction: {e}")

    sent_message = None
    full_text = ""
    last_edit_time = 0
    last_edit_len = 0

    try:
        async for chunk in agent_manager.chat_stream(user_id, message_text):
            full_text += chunk

            # Only edit if enough time has passed AND enough new content
            now = asyncio.get_event_loop().time()
            delta_chars = len(full_text) - last_edit_len

            if delta_chars >= STREAM_MIN_DELTA and (now - last_edit_time) >= STREAM_EDIT_INTERVAL:
                try:
                    display_text = full_text + " ▌"  # Cursor indicator
                    # Truncate if too long for a single message
                    if len(display_text) > 4096:
                        display_text = display_text[-4096:]

                    if sent_message is None:
                        sent_message = await update.message.reply_text(display_text)
                    else:
                        await sent_message.edit_text(display_text)
                    
                    last_edit_time = now
                    last_edit_len = len(full_text)
                except Exception as e:
                    logger.debug(f"Stream edit failed (non-critical): {e}")

    except Exception as e:
        logger.error(f"Streaming error: {e}", exc_info=True)
        full_text = full_text or f"❌ Error: {str(e)}"

    # Remove reaction
    try:
        await update.message.set_reaction(reaction=[])
    except Exception:
        pass

    # Final edit with complete response
    if not full_text.strip():
        full_text = "✅ Done (No text output from AGY)"

    try:
        chunks = split_message(full_text)
        # Edit the first message or send it if it doesn't exist
        try:
            if sent_message is None:
                sent_message = await update.message.reply_text(chunks[0], parse_mode="Markdown")
            else:
                await sent_message.edit_text(chunks[0], parse_mode="Markdown")
        except Exception:
            if sent_message is None:
                sent_message = await update.message.reply_text(chunks[0])
            else:
                await sent_message.edit_text(chunks[0])

        # Send additional messages if response was split
        for chunk in chunks[1:]:
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)

    except Exception as e:
        logger.error(f"Final message edit failed: {e}")
        try:
            if sent_message is not None:
                await sent_message.edit_text(full_text[:4096])
            else:
                await update.message.reply_text(full_text[:4096])
        except Exception:
            pass

    # Save to DB
    if db and conversation_id and full_text.strip():
        await db.add_message(conversation_id, "assistant", full_text)


async def _keep_typing(update: Update):
    """Keep sending typing action every 5 seconds until cancelled."""
    try:
        while True:
            await update.message.chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass
