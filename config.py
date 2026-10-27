"""Validated environment configuration. All thresholds are total HOURS."""

from __future__ import annotations

import math
import hashlib
import os
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    pass


def normalize_discord_token(raw: str) -> str:
    """Accept common copy/paste wrappers without exposing credential contents."""
    invisible = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
    token = raw.translate(dict.fromkeys(map(ord, invisible))).strip()
    for _ in range(8):
        before = token
        token = re.sub(r"^DISCORD_TOKEN\s*=\s*", "", token, flags=re.IGNORECASE).strip()
        if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
            token = token[1:-1].strip()
        token = re.sub(r"^Bot\s+", "", token, flags=re.IGNORECASE).strip()
        if token == before:
            break
    token = "".join(character for character in token if not character.isspace())
    if not token or token in {"DEIN_BOT_TOKEN", "YOUR_BOT_TOKEN"}:
        raise ConfigError("DISCORD_TOKEN is missing. Enter the bot token in Railway Variables.")
    if token.isdigit():
        raise ConfigError("DISCORD_TOKEN contains only digits. Use the token from "
                          "Discord Developer Portal > Bot; the server ID belongs in GUILD_ID.")
    if not token.isascii() or any(character in "\"'" for character in token):
        raise ConfigError("DISCORD_TOKEN contains invalid characters or unmatched quotes. "
                          "Copy the complete bot token again.")
    return token


def token_fingerprint(token: str) -> str:
    """A one-way digest to compare deployments; never return token characters."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Level:
    number: int
    role_id: int
    hours: float

    @property
    def seconds(self) -> float:
        return self.hours * 3600


@dataclass(frozen=True)
class WelcomeSettings:
    enabled: bool = False
    channel_id: int | None = None
    include_bots: bool = True
    ping_user: bool = True
    fallback_color: int = 0x8B5CF6
    message: str = "Welcome {user} to **{server}**! You are the {member_count_ordinal} member!"
    card_text: str = ("Welcome {display_name}\nto {server} you are the {member_count_ordinal} member!\n"
                      "Have fun playing, talking and more!")


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    guild_id: int
    levels: tuple[Level, ...]
    data_dir: Path
    role_mode: str
    min_humans: int
    exclude_afk: bool
    exclude_deafened: bool
    ignored_channel_ids: frozenset[int]
    save_interval: int
    level_up_channel_id: int | None
    railway: bool
    welcome: WelcomeSettings = field(default_factory=WelcomeSettings)
    auto_role_id: int | None = None


def _integer(env: Mapping[str, str], name: str, default: int,
             minimum: int, maximum: int) -> int:
    try:
        value = int(env.get(name, str(default)).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _snowflake(raw: str, name: str) -> int:
    value = raw.strip()
    if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**64:
        raise ConfigError(f"{name}: Enter a valid Discord ID (digits only).")
    return int(value)


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, str(default)).strip().lower()
    if raw in {"true", "1", "yes"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    raise ConfigError(f"{name} must be true or false.")


WELCOME_PLACEHOLDERS = {"user", "username", "display_name", "server", "member_count",
                        "member_count_ordinal"}


def _welcome_text(env: Mapping[str, str], name: str, default: str, limit: int) -> str:
    text = env.get(name, default).replace("\\n", "\n").strip()
    if not text or len(text) > limit:
        raise ConfigError(f"{name} requires non-empty text with no more than {limit} characters.")
    try:
        for _, key, spec, conversion in string.Formatter().parse(text):
            if key is not None and (key not in WELCOME_PLACEHOLDERS or spec or conversion):
                raise ValueError(key)
    except ValueError as exc:
        raise ConfigError(f"{name}: Invalid placeholder. Allowed placeholders: "
                          + ", ".join("{" + key + "}" for key in sorted(WELCOME_PLACEHOLDERS))) from exc
    return text


def _welcome_settings(env: Mapping[str, str]) -> WelcomeSettings:
    defaults = WelcomeSettings()
    enabled = _bool(env, "WELCOME_ENABLED", defaults.enabled)
    channel_raw = env.get("WELCOME_CHANNEL_ID", "").strip()
    if enabled and not channel_raw:
        raise ConfigError("WELCOME_CHANNEL_ID is missing. Enter the welcome text channel's ID "
                          "or set WELCOME_ENABLED=false.")
    channel_id = _snowflake(channel_raw, "WELCOME_CHANNEL_ID") if channel_raw else None
    raw_color = env.get("WELCOME_FALLBACK_COLOR", "#8B5CF6").strip().removeprefix("#").removeprefix("0x")
    try:
        if len(raw_color) != 6 or any(character not in string.hexdigits for character in raw_color):
            raise ValueError(raw_color)
        color = int(raw_color, 16)
    except ValueError as exc:
        raise ConfigError("WELCOME_FALLBACK_COLOR requires a hex color, for example #8B5CF6.") from exc
    return WelcomeSettings(
        enabled=enabled, channel_id=channel_id,
        include_bots=_bool(env, "WELCOME_INCLUDE_BOTS", defaults.include_bots),
        ping_user=_bool(env, "WELCOME_PING_USER", defaults.ping_user), fallback_color=color,
        message=_welcome_text(env, "WELCOME_MESSAGE", defaults.message, 1500),
        card_text=_welcome_text(env, "WELCOME_CARD_TEXT", defaults.card_text, 500),
    )


def level_name(number: int) -> str:
    """Keep stored level numbers continuous while displaying OG tiers after Level 5."""
    return f"OG Level {number - 5}" if number > 5 else f"Level {number}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    token = normalize_discord_token(env.get("DISCORD_TOKEN", ""))
    guild_id = _snowflake(env.get("GUILD_ID", ""), "GUILD_ID")
    # Existing installations keep working until the extra roles and hours are configured.
    count = _integer(env, "LEVEL_COUNT", 5, 1, 25)
    default_hours = (1, 5, 15, 40, 100)
    levels: list[Level] = []
    for number in range(1, count + 1):
        role_id = _snowflake(env.get(f"LEVEL_{number}_ROLE_ID", ""),
                             f"LEVEL_{number}_ROLE_ID")
        name = f"LEVEL_{number}_HOURS"
        raw = env.get(name, str(default_hours[number - 1]) if number <= 5 else "").strip()
        if number > 5 and not raw:
            raise ConfigError(f"{name} is required for {level_name(number)}. "
                              f"Set the total voice hours above LEVEL_{number - 1}_HOURS.")
        try:
            hours = float(raw.replace(",", "."))
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number of hours, for example 5 or 0.5.") from exc
        if not math.isfinite(hours) or hours < 0 or not math.isfinite(hours * 3600):
            raise ConfigError(f"{name} must be a finite number greater than or equal to 0.")
        if levels and hours <= levels[-1].hours:
            raise ConfigError(f"{name} must be greater than LEVEL_{number - 1}_HOURS.")
        if any(level.role_id == role_id for level in levels):
            raise ConfigError("Each level requires its own unique role ID.")
        levels.append(Level(number, role_id, hours))

    auto_role_raw = env.get("AUTO_ROLE_ID", "").strip()
    auto_role_id = _snowflake(auto_role_raw, "AUTO_ROLE_ID") if auto_role_raw else None
    if auto_role_id is not None and any(level.role_id == auto_role_id for level in levels):
        raise ConfigError("AUTO_ROLE_ID requires a separate role that is not used as a level role. "
                          "Otherwise, level synchronization would remove it.")

    mode = env.get("ROLE_MODE", "highest").strip().lower()
    if mode not in {"highest", "cumulative"}:
        raise ConfigError("ROLE_MODE must be highest or cumulative.")

    railway = bool(env.get("RAILWAY_ENVIRONMENT_ID") or env.get("RAILWAY_SERVICE_ID"))
    mount = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    data_dir = Path(env.get("DATA_DIR", "").strip() or mount or "./data").resolve()
    if railway:
        if not mount:
            raise ConfigError("Railway: Create a volume mounted at /data for the bot service first. "
                              "Voice times cannot be saved without a volume.")
        mount_dir = Path(mount).resolve()
        if not data_dir.is_relative_to(mount_dir):
            raise ConfigError("DATA_DIR must be on the Railway volume. "
                              "For a volume mounted at /data, set DATA_DIR=/data.")

    ignored = frozenset(
        _snowflake(value, "IGNORED_CHANNEL_IDS")
        for value in env.get("IGNORED_CHANNEL_IDS", "").split(",") if value.strip()
    )
    channel_raw = env.get("LEVEL_UP_CHANNEL_ID", "").strip()
    return Settings(
        token=token, guild_id=guild_id, levels=tuple(levels), data_dir=data_dir,
        role_mode=mode,
        min_humans=_integer(env, "MIN_HUMANS_IN_CHANNEL", 1, 1, 99),
        exclude_afk=_bool(env, "EXCLUDE_AFK_CHANNEL", True),
        exclude_deafened=_bool(env, "EXCLUDE_DEAFENED", False),
        ignored_channel_ids=ignored,
        save_interval=_integer(env, "SAVE_INTERVAL_SECONDS", 30, 5, 300),
        level_up_channel_id=_snowflake(channel_raw, "LEVEL_UP_CHANNEL_ID") if channel_raw else None,
        railway=railway,
        welcome=_welcome_settings(env),
        auto_role_id=auto_role_id,
    )


def achieved_level(total_seconds: float, levels: tuple[Level, ...]) -> int:
    return max((level.number for level in levels if total_seconds >= level.seconds), default=0)


def role_changes(total_seconds: float, levels: tuple[Level, ...], mode: str,
                 current_ids: set[int]) -> tuple[set[int], set[int], int]:
    """Only the explicitly configured level roles can be changed."""
    reached = achieved_level(total_seconds, levels)
    earned = [level for level in levels if level.number <= reached]
    wanted = {level.role_id for level in (earned[-1:] if mode == "highest" else earned)}
    configured = {level.role_id for level in levels}
    return wanted - current_ids, (current_ids & configured) - wanted, reached
