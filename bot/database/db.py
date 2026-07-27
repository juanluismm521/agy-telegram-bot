"""
Database layer — async SQLite for users, conversations, and messages.
"""

import aiosqlite
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database for persisting bot data."""

    def __init__(self, db_path: str = "data/agy_bot.db"):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        """Initialize database connection and create tables."""
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        logger.info(f"Database initialized at {self.db_path}")

    async def close(self):
        """Close database connection."""
        if self._db:
            await self._db.close()
            logger.info("Database connection closed")

    async def _create_tables(self):
        """Create tables if they don't exist."""
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                preferred_model TEXT DEFAULT '',
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                model_used TEXT DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL,
                message_count INTEGER DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                tokens_used INTEGER DEFAULT 0,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp);
        """)
        await self._db.commit()

    # === User operations ===

    async def upsert_user(self, telegram_id: int, username: str = ""):
        """Create or update a user record."""
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO users (telegram_id, username, created_at, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                last_seen = excluded.last_seen
            """,
            (telegram_id, username, now, now),
        )
        await self._db.commit()

    async def get_user(self, telegram_id: int) -> dict | None:
        """Get user by Telegram ID."""
        async with self._db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_preferred_model(self, telegram_id: int, model: str):
        """Update user's preferred model."""
        await self._db.execute(
            "UPDATE users SET preferred_model = ? WHERE telegram_id = ?",
            (model, telegram_id),
        )
        await self._db.commit()

    async def get_preferred_model(self, telegram_id: int) -> str:
        """Get user's preferred model."""
        async with self._db.execute(
            "SELECT preferred_model FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["preferred_model"] if row else ""

    # === Conversation operations ===

    async def create_conversation(self, telegram_id: int, model: str = "") -> int:
        """Create a new conversation and return its ID."""
        now = time.time()
        async with self._db.execute(
            "INSERT INTO conversations (telegram_id, model_used, started_at) VALUES (?, ?, ?)",
            (telegram_id, model, now),
        ) as cursor:
            conv_id = cursor.lastrowid
        await self._db.commit()
        return conv_id

    async def end_conversation(self, conversation_id: int):
        """Mark a conversation as ended."""
        now = time.time()
        await self._db.execute(
            "UPDATE conversations SET ended_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        await self._db.commit()

    async def get_active_conversation(self, telegram_id: int) -> dict | None:
        """Get the user's most recent active (non-ended) conversation."""
        async with self._db.execute(
            """
            SELECT * FROM conversations
            WHERE telegram_id = ? AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # === Message operations ===

    async def add_message(
        self, conversation_id: int, role: str, content: str, tokens_used: int = 0
    ):
        """Add a message to a conversation."""
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO messages (conversation_id, role, content, timestamp, tokens_used)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, role, content, now, tokens_used),
        )
        # Update message count
        await self._db.execute(
            """
            UPDATE conversations SET message_count = message_count + 1
            WHERE id = ?
            """,
            (conversation_id,),
        )
        await self._db.commit()

    async def get_messages(
        self, conversation_id: int, limit: int = 50
    ) -> list[dict]:
        """Get messages from a conversation, most recent first."""
        async with self._db.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (conversation_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in reversed(rows)]

    # === Stats / Quota ===

    async def get_usage_stats(self, telegram_id: int) -> dict:
        """Get usage statistics for a user."""
        now = time.time()
        day_ago = now - 86400
        week_ago = now - 604800
        month_ago = now - 2592000

        stats = {}

        # Total messages
        async with self._db.execute(
            """
            SELECT COUNT(*) as count FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.telegram_id = ? AND m.role = 'user'
            """,
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            stats["total_messages"] = row["count"]

        # Messages last 24h
        async with self._db.execute(
            """
            SELECT COUNT(*) as count FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.telegram_id = ? AND m.role = 'user' AND m.timestamp > ?
            """,
            (telegram_id, day_ago),
        ) as cursor:
            row = await cursor.fetchone()
            stats["messages_24h"] = row["count"]

        # Messages last 7 days
        async with self._db.execute(
            """
            SELECT COUNT(*) as count FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.telegram_id = ? AND m.role = 'user' AND m.timestamp > ?
            """,
            (telegram_id, week_ago),
        ) as cursor:
            row = await cursor.fetchone()
            stats["messages_7d"] = row["count"]

        # Messages last 30 days
        async with self._db.execute(
            """
            SELECT COUNT(*) as count FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.telegram_id = ? AND m.role = 'user' AND m.timestamp > ?
            """,
            (telegram_id, month_ago),
        ) as cursor:
            row = await cursor.fetchone()
            stats["messages_30d"] = row["count"]

        # Total tokens (estimated)
        async with self._db.execute(
            """
            SELECT COALESCE(SUM(m.tokens_used), 0) as total FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.telegram_id = ?
            """,
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            stats["total_tokens"] = row["total"]

        # Total conversations
        async with self._db.execute(
            "SELECT COUNT(*) as count FROM conversations WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            stats["total_conversations"] = row["count"]

        # Per-model usage
        async with self._db.execute(
            """
            SELECT c.model_used, COUNT(*) as count, COALESCE(SUM(m.tokens_used), 0) as tokens 
            FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.telegram_id = ? AND m.role = 'user'
            GROUP BY c.model_used
            """,
            (telegram_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            stats["models"] = {
                row["model_used"] or "default": {
                    "messages": row["count"],
                    "tokens": row["tokens"]
                } 
                for row in rows
            }

        return stats

    async def clear_history(self, telegram_id: int) -> int:
        """Delete all conversations and messages for a user. Returns count deleted."""
        # Get conversation IDs
        async with self._db.execute(
            "SELECT id FROM conversations WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            conv_ids = [row["id"] for row in rows]

        if not conv_ids:
            return 0

        placeholders = ",".join("?" * len(conv_ids))

        # Delete messages
        await self._db.execute(
            f"DELETE FROM messages WHERE conversation_id IN ({placeholders})",
            conv_ids,
        )

        # Delete conversations
        await self._db.execute(
            f"DELETE FROM conversations WHERE id IN ({placeholders})",
            conv_ids,
        )

        await self._db.commit()
        return len(conv_ids)
