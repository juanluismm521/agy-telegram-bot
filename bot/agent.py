"""
AGY Agent Wrapper — manages Antigravity agent sessions per Telegram user.

Uses the native `agy` CLI binary via subprocess to ensure it uses the machine's 
existing Google AI Pro authentication, without requiring API keys.
"""

import asyncio
import logging
import time
import os
from dataclasses import dataclass, field

from bot.config import get_agy_bin

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    """Tracks an active AGY session for a single Telegram user."""

    user_id: int
    project_id: str
    model: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    is_busy: bool = False

    def touch(self):
        """Update last active timestamp."""
        self.last_active = time.time()


class AGYSessionManager:
    """
    Manages AGY CLI sessions for multiple Telegram users.

    Uses `agy --project <project_id> --continue` to maintain conversation context.
    """

    def __init__(self, default_model: str = "", session_timeout_minutes: int = 60):
        self._sessions: dict[int, UserSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._default_model = default_model
        self._session_timeout = session_timeout_minutes * 60
        self._cleanup_task: asyncio.Task | None = None

        # Find the agy binary — get_agy_bin() also checks the usual install paths,
        # so the bot still works when the daemon starts without ~/.local/bin on PATH.
        self._agy_bin = get_agy_bin()
        if not self._agy_bin:
            logger.warning("AGY CLI not found! Bot will not be able to generate responses.")

    async def start(self):
        """Start the session manager and background cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("AGY CLI Session Manager started")

    async def stop(self):
        """Stop the session manager."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._sessions.clear()
        logger.info("AGY CLI Session Manager stopped")

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def get_or_create_session(self, user_id: int, model: str = "") -> UserSession:
        lock = self._get_lock(user_id)
        async with lock:
            if user_id in self._sessions:
                session = self._sessions[user_id]
                session.touch()
                return session

            # Create a new session (unique project ID)
            project_id = f"tg_{user_id}_{int(time.time())}"
            model_to_use = model or self._default_model

            session = UserSession(
                user_id=user_id,
                project_id=project_id,
                model=model_to_use,
            )
            self._sessions[user_id] = session
            logger.info(f"Session created for user {user_id} (project: {project_id}, model: {model_to_use})")
            return session

    def _build_agy_command(self, session: UserSession, message: str) -> list[str]:
        """Build the command list to run AGY CLI."""
        cmd = [
            self._agy_bin or "agy",
            "--project", session.project_id,
            "--dangerously-skip-permissions",
            "--print-timeout", f"{int(self._session_timeout / 60)}m",
            "--print", message
        ]
        if session.model and session.model != "default":
            cmd.extend(["--model", session.model])
        return cmd

    async def chat(self, user_id: int, message: str) -> str:
        """Send a message to AGY natively and get the full response."""
        session = await self.get_or_create_session(user_id)

        if session.is_busy:
            return "⏳ I am still processing your last request, please wait..."

        if not self._agy_bin:
            return "❌ Erreur interne : La commande `agy` n'est pas installée ou introuvable sur le serveur."

        session.is_busy = True
        try:
            cmd = self._build_agy_command(session, message)
            
            # Run the CLI
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                err = stderr.decode('utf-8').strip()
                logger.error(f"AGY CLI error: {err}")
                return f"❌ AGY Error: {err or 'Unknown CLI error'}"
            
            session.message_count += 1
            session.touch()
            return stdout.decode('utf-8').strip()

        except Exception as e:
            logger.error(f"AGY chat error for user {user_id}: {e}", exc_info=True)
            return f"❌ AGY Error: {str(e)}"
        finally:
            session.is_busy = False

    async def chat_stream(self, user_id: int, message: str):
        """Send a message to AGY natively and yield response chunks as they arrive."""
        session = await self.get_or_create_session(user_id)

        if session.is_busy:
            yield "⏳ I am still processing your last request, please wait..."
            return

        if not self._agy_bin:
            yield "❌ Erreur interne : La commande `agy` n'est pas installée."
            return

        session.is_busy = True
        try:
            cmd = self._build_agy_command(session, message)
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if not process.stdout:
                yield "❌ Error: Could not read from AGY CLI"
                return

            full_text = ""
            while True:
                # Read chunks dynamically (100 bytes at a time for smooth streaming)
                chunk = await process.stdout.read(100)
                if not chunk:
                    break
                
                text_chunk = chunk.decode('utf-8', errors='replace')
                full_text += text_chunk
                yield text_chunk

            await process.wait()

            if process.returncode != 0:
                stderr = await process.stderr.read()
                err = stderr.decode('utf-8').strip()
                logger.error(f"AGY CLI error during streaming: {err}")
                if not full_text:
                    yield f"❌ AGY Error: {err or 'Unknown CLI error'}"
            else:
                session.message_count += 1
                session.touch()

        except Exception as e:
            logger.error(f"AGY stream error for user {user_id}: {e}", exc_info=True)
            yield f"❌ AGY Error: {str(e)}"
        finally:
            session.is_busy = False

    async def new_session(self, user_id: int, model: str = "") -> str:
        old_model = ""
        if user_id in self._sessions:
            old_model = self._sessions[user_id].model
            await self.destroy_session(user_id)

        use_model = model or old_model
        await self.get_or_create_session(user_id, use_model)
        return f"🔄 New session created!\n🤖 Model: {use_model or 'AGY default'}"

    async def change_model(self, user_id: int, model: str) -> str:
        if user_id in self._sessions and self._sessions[user_id].is_busy:
            return "⏳ Cannot change model while processing a request."

        await self.destroy_session(user_id)
        await self.get_or_create_session(user_id, model)
        return f"✅ Model changed: **{model}**\nNew session started."

    async def destroy_session(self, user_id: int):
        if user_id in self._sessions:
            del self._sessions[user_id]
            logger.info(f"Session closed for user {user_id}")

    def get_session_info(self, user_id: int) -> dict | None:
        if user_id not in self._sessions:
            return None

        session = self._sessions[user_id]
        uptime = time.time() - session.created_at
        idle = time.time() - session.last_active

        return {
            "model": session.model,
            "message_count": session.message_count,
            "uptime_minutes": round(uptime / 60, 1),
            "idle_minutes": round(idle / 60, 1),
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "is_busy": session.is_busy,
        }

    def get_all_sessions_info(self) -> dict:
        return {
            "active_sessions": len(self._sessions),
            "sessions": {
                uid: self.get_session_info(uid) for uid in self._sessions
            },
        }

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(300)
                now = time.time()
                expired = [
                    uid for uid, session in self._sessions.items()
                    if (now - session.last_active) > self._session_timeout and not session.is_busy
                ]
                for uid in expired:
                    logger.info(f"Cleaning up inactive session for user {uid}")
                    await self.destroy_session(uid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Session cleanup error: {e}")
