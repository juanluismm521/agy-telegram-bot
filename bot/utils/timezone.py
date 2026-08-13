"""Server-local timezone, resolved once and cached on disk.

This VPS runs its OS clock on UTC, so `datetime.astimezone()` on its own would keep
rendering reset times as UTC no matter where the machine physically sits. What a user
actually wants to read is wall-clock time where the server is, so the zone is resolved
once — via IP geolocation — and cached; every later lookup is in-memory with no network
involved.

The cache stores the IANA *zone name* (e.g. `Europe/Madrid`), never a fixed offset, so
summer/winter time keeps being applied correctly long after detection.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

GEO_URL = "http://ip-api.com/json/?fields=status,timezone"
GEO_TIMEOUT_SECONDS = 6
CACHE_PATH = Path("~/.agy-telegram-bot/timezone.json").expanduser()

_cached_tz: tzinfo | None = None
_cached_name: str = "UTC"


def _load(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone name %r — ignoring.", name)
        return None


def _remember(tz: tzinfo, name: str) -> tzinfo:
    global _cached_tz, _cached_name
    _cached_tz, _cached_name = tz, name
    return tz


def _read_cache() -> str | None:
    try:
        return json.loads(CACHE_PATH.read_text()).get("timezone")
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(name: str, source: str):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"timezone": name, "source": source, "detected_at": time.time()}, indent=2)
        )
    except OSError as e:
        logger.warning("Could not cache the detected timezone: %s", e)


def _system_tz() -> tuple[tzinfo, str] | None:
    """The OS zone, but only when it is genuinely configured — UTC here means 'unset'."""
    name = None
    try:
        name = Path("/etc/timezone").read_text().strip()
    except OSError:
        link = Path("/etc/localtime")
        if link.is_symlink():
            parts = str(link.resolve()).split("/zoneinfo/")
            name = parts[1] if len(parts) > 1 else None

    if not name or name in ("UTC", "Etc/UTC", "Universal"):
        return None
    tz = _load(name)
    return (tz, name) if tz else None


def local_tz() -> tzinfo:
    """The server's timezone. Never touches the network — safe to call from anywhere."""
    if _cached_tz is not None:
        return _cached_tz

    override = os.getenv("BOT_TIMEZONE", "").strip()
    tz = _load(override)
    if tz:
        return _remember(tz, override)

    cached = _read_cache()
    tz = _load(cached)
    if tz:
        return _remember(tz, cached)

    system = _system_tz()
    if system:
        return _remember(*system)

    return _remember(timezone.utc, "UTC")


def tz_name() -> str:
    local_tz()
    return _cached_name


def tz_label() -> str:
    """Short label for display, e.g. `CEST` — the abbreviation in effect right now."""
    return datetime.now(local_tz()).strftime("%Z") or tz_name()


async def warm_timezone() -> str:
    """Resolve the timezone at startup, geolocating once if nothing is cached yet."""
    override = os.getenv("BOT_TIMEZONE", "").strip()
    tz = _load(override)
    if tz:
        _remember(tz, override)
        logger.info("Timezone %s (from BOT_TIMEZONE).", override)
        return override

    cached = _read_cache()
    tz = _load(cached)
    if tz:
        _remember(tz, cached)
        logger.info("Timezone %s (cached).", cached)
        return cached

    name = await _detect_via_ip()
    if name:
        tz = _load(name)
        if tz:
            _remember(tz, name)
            _write_cache(name, "ip-geolocation")
            logger.info("Timezone %s (detected by IP geolocation, cached).", name)
            return name

    system = _system_tz()
    if system:
        _remember(*system)
        logger.info("Timezone %s (from the OS).", system[1])
        return system[1]

    _remember(timezone.utc, "UTC")
    logger.warning("Could not determine the server timezone — falling back to UTC.")
    return "UTC"


async def _detect_via_ip() -> str | None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=GEO_TIMEOUT_SECONDS) as client:
            resp = await client.get(GEO_URL)
        if resp.status_code != 200:
            logger.warning("Geolocation lookup returned HTTP %s.", resp.status_code)
            return None
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - detection is best-effort, never fatal
        logger.warning("Geolocation lookup failed (%s) — falling back.", e)
        return None

    if data.get("status") != "success":
        return None
    return data.get("timezone") or None


def to_local(dt: datetime) -> datetime:
    """Move an aware (or assumed-UTC) datetime into the server's timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())
