"""
Daemon management — PID file, log file, background process control.
"""

import os
import sys
import signal
import subprocess
import time
import logging

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_PID_FILE = os.path.expanduser("~/.agy-telegram-bot/bot.pid")
DEFAULT_LOG_FILE = os.path.expanduser("~/.agy-telegram-bot/bot.log")
DEFAULT_DATA_DIR = os.path.expanduser("~/.agy-telegram-bot")


def ensure_dirs():
    """Ensure the data directory exists."""
    os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)


# If start/restart is invoked from inside a live Claude Code session (e.g. this
# bot's own agent handling a "restart yourself" request), subprocess.Popen would
# otherwise hand the new daemon that session's internal identity — confusing
# whatever CLI the daemon itself shells out to.
_LEAKED_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE")
_LEAKED_ENV_KEYS = {"CLAUDE_PID", "CLAUDE_EFFORT", "SUDO_COMMAND", "AI_AGENT", "IS_SANDBOX"}


def _clean_env() -> dict:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_LEAKED_ENV_PREFIXES) and k not in _LEAKED_ENV_KEYS
    }


def get_pid() -> int | None:
    """Read the PID from the PID file. Returns None if not running."""
    if not os.path.exists(DEFAULT_PID_FILE):
        return None

    try:
        with open(DEFAULT_PID_FILE, "r") as f:
            pid = int(f.read().strip())

        # Check if process is actually running
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # PID file exists but process is dead — clean up
        _remove_pid_file()
        return None


def write_pid(pid: int):
    """Write PID to file."""
    ensure_dirs()
    with open(DEFAULT_PID_FILE, "w") as f:
        f.write(str(pid))


def _remove_pid_file():
    """Remove the PID file."""
    try:
        os.remove(DEFAULT_PID_FILE)
    except FileNotFoundError:
        pass


def start_daemon(project_dir: str) -> tuple[bool, str]:
    """
    Start the bot as a background daemon.

    Returns:
        (success, message)
    """
    pid = get_pid()
    if pid:
        return False, f"Bot is already running (PID {pid})"

    ensure_dirs()

    # Find the venv python
    venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
    if not os.path.exists(venv_python):
        venv_python = os.path.join(project_dir, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    # Check .env exists
    env_file = os.path.join(project_dir, ".env")
    if not os.path.exists(env_file):
        return False, (
            "❌ .env file not found!\n"
            "Run `agy-telegram-bot setup` first to configure the bot."
        )

    # Start the bot process in background
    log_file = open(DEFAULT_LOG_FILE, "a")

    process = subprocess.Popen(
        [venv_python, "-m", "bot.main"],
        cwd=project_dir,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # Detach from terminal
        env=_clean_env(),
    )

    write_pid(process.pid)

    # Wait a moment and check if it's still running
    time.sleep(1.5)
    if process.poll() is not None:
        _remove_pid_file()
        return False, (
            f"❌ Bot crashed on startup (exit code: {process.returncode})\n"
            f"Check logs: agy-telegram-bot logs"
        )

    return True, f"✅ Bot started (PID {process.pid})"


def stop_daemon() -> tuple[bool, str]:
    """
    Stop the running bot daemon.

    Returns:
        (success, message)
    """
    pid = get_pid()
    if not pid:
        return False, "Bot is not running."

    try:
        # Send SIGTERM for graceful shutdown
        os.kill(pid, signal.SIGTERM)

        # Wait for it to stop (max 10 seconds)
        for _ in range(20):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                _remove_pid_file()
                return True, f"✅ Bot stopped (was PID {pid})"

        # Force kill if still running
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        _remove_pid_file()
        return True, f"⚠️ Bot force-killed (was PID {pid})"

    except ProcessLookupError:
        _remove_pid_file()
        return True, "Bot was already stopped."
    except PermissionError:
        return False, f"❌ Permission denied to stop PID {pid}"


def get_status() -> dict:
    """Get the current daemon status."""
    pid = get_pid()
    info = {
        "running": pid is not None,
        "pid": pid,
        "pid_file": DEFAULT_PID_FILE,
        "log_file": DEFAULT_LOG_FILE,
    }

    if pid:
        # Get process uptime
        try:
            stat_file = f"/proc/{pid}/stat"
            if os.path.exists(stat_file):
                boot_time = _get_boot_time()
                with open(stat_file, "r") as f:
                    stat = f.read().split()
                    start_ticks = int(stat[21])
                    clock_ticks = os.sysconf("SC_CLK_TCK")
                    start_time = boot_time + (start_ticks / clock_ticks)
                    uptime = time.time() - start_time
                    info["uptime_seconds"] = uptime
                    info["uptime_human"] = _format_duration(uptime)
        except Exception:
            pass

        # Get memory usage
        try:
            status_file = f"/proc/{pid}/status"
            if os.path.exists(status_file):
                with open(status_file, "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_kb = int(line.split()[1])
                            info["memory_mb"] = round(mem_kb / 1024, 1)
                            break
        except Exception:
            pass

    # Log file size
    if os.path.exists(DEFAULT_LOG_FILE):
        size = os.path.getsize(DEFAULT_LOG_FILE)
        info["log_size_mb"] = round(size / (1024 * 1024), 2)

    return info


def _get_boot_time() -> float:
    """Get system boot time from /proc/stat."""
    with open("/proc/stat", "r") as f:
        for line in f:
            if line.startswith("btime"):
                return float(line.split()[1])
    return 0


def _format_duration(seconds: float) -> str:
    """Format seconds into human readable duration."""
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")

    return " ".join(parts)
