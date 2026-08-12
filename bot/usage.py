"""
AGY plan limits — the data behind AGY's own `/usage` slash command.

The AGY CLI expands slash commands in print mode (see `--disable-slash-commands`),
so `agy -p "/usage"` returns the limits as tab-separated rows:

    Gemini Models\tWeekly Limit Remaining\t97%\t2026-08-19T07:42:54Z
    Claude and GPT models\tFive Hour Limit Remaining\tdisabled\t

Note the figures are *remaining*, not consumed.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from bot.config import get_agy_bin

logger = logging.getLogger(__name__)

USAGE_TIMEOUT_SECONDS = 60
# "Five Hour Limit Remaining" -> "Five Hour"
LABEL_SUFFIX = re.compile(r"\s*limit\s+remaining\s*$", re.IGNORECASE)
# Shorter windows first; anything unrecognised keeps CLI order at the end.
WINDOW_ORDER = {"five hour": 0, "weekly": 1}


class UsageError(Exception):
    """AGY plan limits could not be retrieved."""


async def _run_agy_usage() -> str:
    agy_bin = get_agy_bin()
    if not agy_bin:
        raise UsageError("The `agy` CLI was not found on this server.")

    try:
        process = await asyncio.create_subprocess_exec(
            agy_bin,
            "-p",
            "/usage",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=USAGE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        raise UsageError(f"The AGY CLI did not respond within {USAGE_TIMEOUT_SECONDS}s.") from None
    except OSError as e:
        raise UsageError(f"Could not run the AGY CLI: {e}") from None

    output = stdout.decode(errors="replace")
    errors = stderr.decode(errors="replace")

    if "not signed in" in (output + errors).lower():
        raise UsageError("The AGY CLI is not signed in — run `agy prompt --print hi` on the server.")
    if process.returncode != 0 and not output.strip():
        detail = errors.strip() or f"exit code {process.returncode}"
        raise UsageError(f"The AGY CLI failed: {detail}")
    return output


def parse_usage(raw: str) -> list[dict]:
    """Parse the tab-separated `/usage` rows into limit entries."""
    entries = []
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        columns = [c.strip() for c in line.split("\t")]
        family, label = columns[0], columns[1] if len(columns) > 1 else ""
        value = columns[2] if len(columns) > 2 else ""
        if not family or not label:
            continue

        window = LABEL_SUFFIX.sub("", label).strip() or label
        percent = None
        if value.endswith("%"):
            try:
                percent = float(value[:-1])
            except ValueError:
                percent = None

        entries.append(
            {
                "family": family,
                "window": window,
                "remaining_percent": percent,
                "raw_value": value,
                "resets_at": columns[3] if len(columns) > 3 else "",
            }
        )
    return entries


def _bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    # Keep partial values visually distinct from the 0% / 100% extremes.
    if filled == width and percent < 100:
        filled = width - 1
    elif filled == 0 and percent > 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def _reset_note(iso: str) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = int((dt - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "resetting now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        relative = f"{days}d {hours}h"
    elif hours:
        relative = f"{hours}h {minutes}m"
    else:
        relative = f"{minutes}m"
    return f"resets in {relative} · {dt.astimezone(timezone.utc):%b %d, %H:%M} UTC"


def format_usage(entries: list[dict]) -> str:
    """Render parsed limits as a Telegram Markdown message."""
    if not entries:
        return "*📊 AGY Plan Limits*\n\n⚠️ The AGY CLI returned no limit data."

    lines = ["*📊 AGY Plan Limits*", "_Figures are quota remaining._\n"]

    families: dict[str, list[dict]] = {}
    for entry in entries:
        families.setdefault(entry["family"], []).append(entry)

    for family, group in families.items():
        lines.append(f"*{family}*")
        for entry in sorted(group, key=lambda e: WINDOW_ORDER.get(e["window"].lower(), 99)):
            percent = entry["remaining_percent"]
            if percent is None:
                lines.append(f"⚪ {entry['window']} — {entry['raw_value'] or 'unavailable'}")
                continue
            dot = "🔴" if percent <= 10 else "🟡" if percent <= 30 else "🟢"
            lines.append(f"{dot} {entry['window']} — *{percent:.0f}%* left")
            lines.append(f"`{_bar(percent)}`")
            note = _reset_note(entry["resets_at"])
            if note:
                lines.append(f"_{note}_")
        lines.append("")

    return "\n".join(lines).rstrip()


async def get_usage_message() -> str:
    """Fetch and render AGY plan limits, or return a readable error message."""
    try:
        raw = await _run_agy_usage()
    except UsageError as e:
        return f"*📊 AGY Plan Limits*\n\n⚠️ {e}"

    entries = parse_usage(raw)
    if not entries:
        logger.warning("Unrecognised /usage output from AGY: %r", raw[:500])
        snippet = raw.strip()[:500] or "(empty response)"
        return f"*📊 AGY Plan Limits*\n\n⚠️ Could not parse the AGY response:\n\n```\n{snippet}\n```"
    return format_usage(entries)
