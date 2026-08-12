"""
Configuration module — loads environment variables and defines constants.
"""

import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


import subprocess
import shutil
import os

def get_agy_bin() -> str | None:
    """Find the agy binary path."""
    bin_path = shutil.which("agy")
    if bin_path:
        return bin_path
    
    # Common fallback paths
    fallbacks = [
        os.path.expanduser("~/.local/bin/agy"),
        os.path.expanduser("~/.gemini/bin/agy"),
        "/usr/local/bin/agy"
    ]
    for p in fallbacks:
        if os.path.exists(p):
            return p
    return None

def get_available_models() -> list[str]:
    """Dynamically fetch available models from the AGY CLI."""
    agy_bin = get_agy_bin()
    if not agy_bin:
        logger.error("AGY CLI not found. Cannot fetch models.")
        return []

    try:
        result = subprocess.run(
            [agy_bin, "models"], 
            capture_output=True, 
            text=True, 
            check=False
        )
        
        output = result.stdout + result.stderr
        if "not signed in" in output.lower():
            logger.error("AGY CLI is not signed in! Run `agy prompt --print hi` to authenticate.")
            return []
            
        # Parse output into a list of strings, removing empty lines and non-model lines
        models = [line.split("\t")[0].strip() for line in result.stdout.split("\n") if line.strip() and not line.startswith("Welcome") and not line.startswith("▀")]
        return models
    except Exception as e:
        logger.error(f"Failed to fetch models from AGY CLI: {e}")
        return ["Gemini 3.1 Pro (High)"]  # Safe default fallback

# Telegram message limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_MAX_CAPTION_LENGTH = 1024


@dataclass
class Settings:
    """Bot configuration loaded from environment variables."""

    # Telegram
    telegram_bot_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)

    # AGY
    default_model: str = ""

    # Behavior
    log_level: str = "INFO"
    max_context_messages: int = 50
    session_timeout_minutes: int = 60
    enable_streaming: bool = True

    # Database
    db_path: str = "data/agy_bot.db"

    def __post_init__(self):
        """Load and validate settings from environment."""
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.default_model = os.getenv("DEFAULT_MODEL", "")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.max_context_messages = int(os.getenv("MAX_CONTEXT_MESSAGES", "50"))
        self.session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "60"))
        self.enable_streaming = os.getenv("ENABLE_STREAMING", "true").lower() == "true"

        # Parse allowed user IDs
        raw_ids = os.getenv("ALLOWED_USER_IDS", "")
        if raw_ids:
            self.allowed_user_ids = [
                int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()
            ]

        self._validate()

    def _validate(self):
        """Validate required configuration."""
        if not self.telegram_bot_token or self.telegram_bot_token == "your_bot_token_here":
            raise ValueError(
                "❌ TELEGRAM_BOT_TOKEN is not set! "
                "Get one from @BotFather on Telegram and add it to .env"
            )

        if not self.allowed_user_ids:
            logger.warning(
                "⚠️  ALLOWED_USER_IDS is empty — bot will reject ALL messages! "
                "Add your Telegram User ID to .env"
            )

    @property
    def log_level_int(self) -> int:
        """Convert log level string to logging constant."""
        return getattr(logging, self.log_level.upper(), logging.INFO)


def load_settings() -> Settings:
    """Create and return a Settings instance."""
    return Settings()
