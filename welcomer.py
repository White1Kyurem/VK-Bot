"""Reference-style welcome cards with the fixed Unturned background."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import discord
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from config import Settings, WelcomeSettings

log = logging.getLogger("voice-level-bot.welcomer")
CARD_SIZE = (1000, 320)
SCALE = 2
BACKGROUND_PATH = Path(__file__).resolve().parent / "assets" / "welcome-background.png"


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def member_values(member: discord.Member) -> dict[str, str]:
    count = member.guild.member_count
    if count is None:
        count = len(member.guild.members)
    return {"user": member.mention, "username": member.name, "display_name": member.display_name,
            "server": member.guild.name, "member_count": str(count),
            "member_count_ordinal": ordinal(count)}


def dominant_color(avatar: Image.Image, fallback: int) -> tuple[int, int, int]:
    sample = avatar.convert("RGBA")
    sample.thumbnail((96, 96), Image.Resampling.LANCZOS)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for red, green, blue, alpha in sample.get_flattened_data():
        if alpha < 128:
            continue
        # Nearby shades belong to the same colour, so gradients do not defeat the mode.
        bucket = buckets.setdefault((red // 32, green // 32, blue // 32), [0, 0, 0, 0])
        bucket[0] += 1
        bucket[1] += red
        bucket[2] += green
        bucket[3] += blue
    if not buckets:
        return (fallback >> 16 & 255, fallback >> 8 & 255, fallback & 255)
    count, red, green, blue = max(buckets.values(), key=lambda bucket: bucket[0])
    return (round(red / count), round(green / count), round(blue / count))


def get_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def wrap_lines(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = []
    for paragraph in text.splitlines() or [text]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            # Also handle very long usernames/server names without spaces.
            for character in word:
                if line and font.getlength(line + character) > width:
                    lines.append(line)
                    line = ""
                line += character
        lines.append(line)
    return lines


def fit_text(text: str, width: int, height: int):
    paragraphs = text.splitlines() or [text]
    # Preserve the reference's three centered lines whenever they fit.
    for size in range(34, 15, -2):
        font = get_font(size * SCALE)
        line_height = round(size * SCALE * 1.3)
        if (all(font.getlength(line) <= width for line in paragraphs)
                and len(paragraphs) * line_height <= height):
            return font, paragraphs, line_height
    for size in range(34, 15, -2):
        font = get_font(size * SCALE)
        lines = wrap_lines(text, font, width)
        line_height = round(size * SCALE * 1.3)
        if len(lines) * line_height <= height:
            return font, lines, line_height
    font = get_font(16 * SCALE)
    line_height = round(16 * SCALE * 1.3)
    lines = wrap_lines(text, font, width)
    maximum = max(1, height // line_height)
    if len(lines) > maximum:
        lines = lines[:maximum]
        last = lines[-1]
        while last and font.getlength(last + "…") > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return font, lines, line_height


def render_card(avatar_bytes: bytes | None, text: str, fallback: int) -> tuple[bytes, int]:
    fallback_rgb = (fallback >> 16 & 255, fallback >> 8 & 255, fallback & 255)
    avatar = None
    if avatar_bytes:
        try:
            with Image.open(io.BytesIO(avatar_bytes)) as source:
                avatar = ImageOps.exif_transpose(source).convert("RGBA")
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            log.warning("The avatar could not be read; the welcome card is using the fallback color.")
    accent = dominant_color(avatar, fallback) if avatar else fallback_rgb
    color = accent[0] << 16 | accent[1] << 8 | accent[2]
    width, height = CARD_SIZE[0] * SCALE, CARD_SIZE[1] * SCALE
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=10 * SCALE, fill="white")
    border = 14 * SCALE
    with Image.open(BACKGROUND_PATH) as source:
        wallpaper = ImageOps.fit(source.convert("RGBA"), (width - 2 * border, height - 2 * border),
                                method=Image.Resampling.LANCZOS)
    canvas.alpha_composite(wallpaper, (border, border))

    left, top, diameter, ring = 48 * SCALE, 50 * SCALE, 220 * SCALE, 8 * SCALE
    draw.ellipse((left - ring, top - ring, left + diameter + ring, top + diameter + ring), fill="white")
    if avatar is None:
        avatar = Image.new("RGBA", (diameter, diameter), accent + (255,))
        ImageDraw.Draw(avatar).text((diameter / 2, diameter / 2), "?", anchor="mm",
                                   font=get_font(90 * SCALE), fill="white",
                                   stroke_width=2 * SCALE, stroke_fill="black")
    avatar = ImageOps.fit(avatar, (diameter, diameter), method=Image.Resampling.LANCZOS)
    circle = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
    canvas.paste(avatar, (left, top), ImageChops.multiply(circle, avatar.getchannel("A")))

    text_left, text_right = 306 * SCALE, 958 * SCALE
    font, lines, line_height = fit_text(text, text_right - text_left - 8 * SCALE, 236 * SCALE)
    x = (text_left + text_right) / 2
    first_y = height / 2 - (len(lines) - 1) * line_height / 2
    for index, line in enumerate(lines):
        draw.text((x, first_y + index * line_height), line, font=font, anchor="mm",
                  fill="white", stroke_width=2 * SCALE, stroke_fill="black")
    canvas = canvas.resize(CARD_SIZE, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    canvas.save(output, "PNG", optimize=True)
    return output.getvalue(), color


def welcome_problems(guild: discord.Guild, settings: WelcomeSettings) -> list[str]:
    if not settings.enabled:
        return []
    if not BACKGROUND_PATH.is_file():
        return ["The welcome background is missing. Upload the complete assets folder to GitHub."]
    channel = guild.get_channel(settings.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return ["WELCOME_CHANNEL_ID must point to a text channel on this server."]
    if guild.me is None:
        return ["The bot's server member could not be loaded."]
    permissions = channel.permissions_for(guild.me)
    names = {"view_channel": "View Channels", "send_messages": "Send Messages",
             "embed_links": "Embed Links", "attach_files": "Attach Files"}
    return [f"Missing permission in the welcome channel: {label}." for flag, label in names.items()
            if not getattr(permissions, flag)]


async def send_welcome(member: discord.Member, settings: Settings) -> bool:
    welcome = settings.welcome
    if not welcome.enabled or member.guild.id != settings.guild_id:
        return False
    if member.bot and not welcome.include_bots:
        return False
    problems = welcome_problems(member.guild, welcome)
    if problems:
        for problem in problems:
            log.warning(problem)
        return False
    values = member_values(member)
    # Mentions are useful in the caption; the image needs a readable name instead.
    card_values = dict(values, user=member.display_name)
    text = welcome.card_text.format_map(card_values)
    content = welcome.message.format_map(values)[:2000]
    avatar_bytes = None
    try:
        avatar = member.display_avatar.with_format("png").with_size(256)
        avatar_bytes = await asyncio.wait_for(avatar.read(), timeout=10)
    except (discord.HTTPException, OSError, ValueError, asyncio.TimeoutError):
        log.warning("Avatar unavailable for user %s; the welcome card is using the fallback color.", member.id)
    png, color = await asyncio.to_thread(render_card, avatar_bytes, text, welcome.fallback_color)
    filename = f"welcome-{member.id}.png"
    file = discord.File(io.BytesIO(png), filename=filename)
    embed = discord.Embed(colour=discord.Colour(color))
    embed.set_image(url=f"attachment://{filename}")
    mentions = discord.AllowedMentions(users=[member] if welcome.ping_user else False,
                                      roles=False, everyone=False, replied_user=False)
    try:
        channel = member.guild.get_channel(welcome.channel_id)
        await channel.send(content=content, embed=embed, file=file, allowed_mentions=mentions)
        return True
    except discord.HTTPException:
        log.warning("The welcome message could not be sent. Check channel permissions.", exc_info=True)
        return False
    finally:
        file.close()
