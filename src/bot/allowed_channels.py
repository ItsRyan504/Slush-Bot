# Per-server channel allowlist with JSON + pinned-message persistence.
#
# Public API used by the bot:
#   init_allowed()
#   allowed_channel_check
#   slash_allowed_check
#   check_failure_reply(interaction, error) -> bool
#   register_allowed_admin_commands(bot)
#
# New features:
#   - /setup: create & pin a config message in the current channel
#   - /reload-config: reload allowed-channel config from pinned message
#   - On startup, the module automatically scans pinned config messages
#     across guilds and restores the allowlist, so you don't have to
#     /allow_here again after each restart.

import os
import json
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord.ext import commands
from discord import app_commands

# Where to store a local copy (for quick reloads between restarts that don't
# wipe the filesystem). On Render this may be ephemeral across deploys, but
# it's still safe and convenient.
DATA_FILE = Path(os.getenv("ALLOWED_DATA_FILE", "allowed_channels.json"))

# Marker string used inside the pinned config message so we can find it again.
PIN_MARKER = "[RBX-GP-BOT-CONFIG]"

# guild_id -> [channel_id, ...]
_allowed_by_guild: Dict[int, List[int]] = {}


# ---------- storage helpers ----------

def _load_allowed() -> None:
    """Load allowlist from JSON on disk (best-effort)."""
    global _allowed_by_guild
    if not DATA_FILE.exists():
        _allowed_by_guild = {}
        return
    try:
        raw = json.loads(DATA_FILE.read_text())
        parsed: Dict[int, List[int]] = {}
        for g, chans in raw.items():
            try:
                gid = int(g)
            except (TypeError, ValueError):
                continue
            parsed[gid] = [int(c) for c in chans]
        _allowed_by_guild = parsed
    except Exception:
        # If file is corrupted or unreadable, just fall back to empty.
        _allowed_by_guild = {}


def _save_allowed() -> None:
    """Persist allowlist to JSON on disk (best-effort)."""
    try:
        serializable = {str(g): list(set(chans)) for g, chans in _allowed_by_guild.items()}
        DATA_FILE.write_text(json.dumps(serializable, indent=2))
    except Exception:
        # Swallow any IO errors; bot should still run.
        pass


def init_allowed() -> None:
    """Called at import-time from the main bot to initialise in-memory state."""
    _load_allowed()


# ---------- core helpers ----------

def guild_allowed_channel_ids(guild_id: int) -> List[int]:
    """Return list of allowed channel IDs for this guild (empty = all allowed)."""
    return _allowed_by_guild.get(guild_id, [])


def guild_channel_is_allowed(guild_id: int, channel_id: int) -> bool:
    """
    If a guild has no explicit allowlist, the bot is allowed in all channels.
    Otherwise, only channels in the list are allowed.
    """
    chans = _allowed_by_guild.get(guild_id)
    if not chans:
        return True
    return channel_id in chans


def set_guild_allowed_channels(guild_id: int, channel_ids: List[int]) -> None:
    """Replace allowlist for a guild and save."""
    _allowed_by_guild[guild_id] = list(dict.fromkeys(int(c) for c in channel_ids))
    _save_allowed()


def add_allowed_channel(guild_id: int, channel_id: int) -> None:
    chans = _allowed_by_guild.get(guild_id, [])
    if channel_id not in chans:
        chans.append(channel_id)
        _allowed_by_guild[guild_id] = chans
        _save_allowed()


def clear_allowed_for_guild(guild_id: int) -> None:
    _allowed_by_guild.pop(guild_id, None)
    _save_allowed()


# ---------- decorators for prefix + slash ----------

def allowed_channel_check(func):
    """
    Decorator for prefix commands. Replies in-channel if command is not allowed.
    """
    @commands.check
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            # DMs: always allowed
            return True
        if guild_channel_is_allowed(ctx.guild.id, ctx.channel.id):
            return True
        try:
            await ctx.reply(
                "❌ This command isn't allowed in this channel. "
                "Ask an admin to use `/setup` or `/allow_here`.",
                mention_author=False,
            )
        except Exception:
            pass
        return False

    return predicate(func)


def _slash_allowed_predicate(interaction: discord.Interaction) -> bool:
    """
    Underlies slash_allowed_check. Raises CheckFailure if channel is not allowed.
    """
    if interaction.guild is None:
        return True
    if guild_channel_is_allowed(interaction.guild.id, interaction.channel.id):  # type: ignore[arg-type]
        return True
    # Mark this as our own "channel not allowed" failure; handled in check_failure_reply
    raise app_commands.CheckFailure("channel_not_allowed")


def slash_allowed_check(func):
    """
    Decorator for slash commands. On failure, raises CheckFailure handled by
    check_failure_reply().
    """
    return app_commands.check(_slash_allowed_predicate)(func)


async def check_failure_reply(interaction: discord.Interaction, error: app_commands.AppCommandError) -> bool:
    """
    Shared error handler used in the main bot file.
    Returns True if we already responded to the user, False otherwise.
    """
    if isinstance(error, app_commands.CheckFailure):
        # If we can still respond, send a generic ephemeral notice.
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ You can't use this command here or you don't have permission.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ You can't use this command here or you don't have permission.",
                    ephemeral=True,
                )
        except Exception:
            pass
        return True
    return False


# ---------- pinned message config ----------

def _encode_config_payload(guild: discord.Guild) -> str:
    data = {
        "guild_id": guild.id,
        "allowed_channels": guild_allowed_channel_ids(guild.id),
    }
    body = json.dumps(data, indent=2)
    return (
        f"{PIN_MARKER}\n"
        "📌 Bot configuration for this server.\n"
        "Do not edit this message manually.\n"
        "The bot will reload allowed channels from here on restart or `/reload-config`.\n\n"
        "```json\n"
        f"{body}\n"
        "```"
    )


def _extract_config_json_from_message(msg: discord.Message) -> Optional[dict]:
    """
    Given a pinned config message, pull out the JSON payload.
    Returns None if parsing fails.
    """
    if PIN_MARKER not in msg.content:
        return None
    text = msg.content

    # Try to locate ```json ... ``` block
    start = text.find("```")
    if start == -1:
        return None
    end = text.find("```", start + 3)
    if end == -1:
        return None
    block = text[start + 3: end]  # may start with 'json\n'
    if block.startswith("json"):
        block = block[4:]  # drop 'json' + newline
    try:
        return json.loads(block.strip())
    except Exception:
        return None


async def _reload_from_pins_for_guild(bot: commands.Bot, guild: discord.Guild) -> bool:
    """
    Look through all text channels for a pinned config message and use it
    to restore the allowlist. Returns True if one was found and loaded.
    """
    for channel in guild.text_channels:
        try:
            pins = await channel.pins()
        except (discord.Forbidden, discord.HTTPException):
            continue

        for msg in pins:
            if msg.author.id != bot.user.id:
                continue
            if PIN_MARKER not in msg.content:
                continue
            data = _extract_config_json_from_message(msg)
            if not data:
                continue
            if int(data.get("guild_id", 0)) != guild.id:
                continue
            allowed = [int(c) for c in data.get("allowed_channels", [])]
            set_guild_allowed_channels(guild.id, allowed)
            return True
    return False


async def reload_allowed_from_pins(bot: commands.Bot) -> None:
    """
    Reload allowlists for all guilds from pinned config messages.
    Intended to be called once after startup.
    """
    for guild in bot.guilds:
        await _reload_from_pins_for_guild(bot, guild)


# ---------- command registration ----------

def register_allowed_admin_commands(bot: commands.Bot) -> None:
    """
    Register admin commands for managing allowed channels and config pins.
    Also schedules a background task to sync from pinned messages once the
    bot is ready (so restart automatically reloads config).
    """

    # ----- background sync on startup -----

    async def _sync_on_ready():
        await bot.wait_until_ready()
        await reload_allowed_from_pins(bot)

    # schedule once; if this fails it's non-fatal
    try:
        bot.loop.create_task(_sync_on_ready())
    except Exception:
        pass

    # ----- prefix commands -----

    @bot.command(name="allow_here", help="(Admin) Allow this channel for bot commands in this server.")
    async def allow_here_prefix(ctx: commands.Context):
        if not ctx.guild:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)

        gid, cid = ctx.guild.id, ctx.channel.id
        add_allowed_channel(gid, cid)
        await ctx.reply(f"✅ Allowed channel set: <#{cid}>", mention_author=False)

    @bot.command(name="clear_allowed", help="(Admin) Clear all allowed-channel restrictions for this server.")
    async def clear_allowed_prefix(ctx: commands.Context):
        if not ctx.guild:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)

        clear_allowed_for_guild(ctx.guild.id)
        await ctx.reply("✅ Cleared restrictions — bot allowed in all channels here.", mention_author=False)

    @bot.command(name="list_allowed", help="(Admin) List allowed channels for this server.")
    async def list_allowed_prefix(ctx: commands.Context):
        if not ctx.guild:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)

        chans = guild_allowed_channel_ids(ctx.guild.id)
        if not chans:
            return await ctx.reply("No restrictions set — bot allowed in all channels.", mention_author=False)
        await ctx.reply("Allowed channels: " + ", ".join(f"<#{c}>" for c in chans), mention_author=False)

    # ----- slash commands (channel allowlist) -----

    @bot.tree.command(name="allow_here", description="(Admin) Allow this channel for bot commands in this server.")
    async def allow_here_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)

        gid, cid = interaction.guild.id, interaction.channel.id  # type: ignore[assignment]
        add_allowed_channel(gid, cid)
        await interaction.response.send_message(f"✅ Allowed channel set: <#{cid}>", ephemeral=True)

    @bot.tree.command(name="clear_allowed", description="(Admin) Clear all allowed-channel restrictions for this server.")
    async def clear_allowed_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)

        clear_allowed_for_guild(interaction.guild.id)
        await interaction.response.send_message("✅ Cleared restrictions — bot allowed in all channels here.", ephemeral=True)

    @bot.tree.command(name="list_allowed", description="(Admin) List allowed channels for this server.")
    async def list_allowed_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)

        chans = guild_allowed_channel_ids(interaction.guild.id)
        if not chans:
            return await interaction.response.send_message("No restrictions set — bot allowed in all channels.", ephemeral=True)
        await interaction.response.send_message("Allowed channels: " + ", ".join(f"<#{c}>" for c in chans), ephemeral=True)

    # ----- new slash commands: /setup and /reload-config -----

    @bot.tree.command(name="setup", description="(Admin) Create & pin the bot config message in this channel.")
    async def setup_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)

        guild = interaction.guild
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Please run this in a text channel.", ephemeral=True)

        content = _encode_config_payload(guild)
        msg = await channel.send(content)
        # Try to pin the message; it's okay if we don't have permission.
        try:
            await msg.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.response.send_message(
            "✅ Setup complete. Config message created "
            "and (if possible) pinned in this channel.\n"
            "The bot will now remember allowed channels across restarts.",
            ephemeral=True,
        )

    @bot.tree.command(name="reload-config", description="(Admin) Reload config from pinned message in this channel.")
    async def reload_config_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)

        guild = interaction.guild
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Please run this in a text channel.", ephemeral=True)

        try:
            pins = await channel.pins()
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                "I couldn't read pinned messages here. Do I have "
                "`Read Message History` and maybe `Manage Messages`?",
                ephemeral=True,
            )

        target: Optional[discord.Message] = None
        for msg in pins:
            if msg.author.id != bot.user.id:
                continue
            if PIN_MARKER not in msg.content:
                continue
            target = msg
            break

        if not target:
            return await interaction.response.send_message(
                "I couldn't find a pinned config message in this channel. "
                "Use `/setup` here first.",
                ephemeral=True,
            )

        data = _extract_config_json_from_message(target)
        if not data:
            return await interaction.response.send_message(
                "Pinned config message exists but could not be parsed. "
                "Try running `/setup` again.",
                ephemeral=True,
            )

        if int(data.get("guild_id", 0)) != guild.id:
            return await interaction.response.send_message(
                "The pinned config message seems to belong to a different server. "
                "Try running `/setup` again.",
                ephemeral=True,
            )

        allowed = [int(c) for c in data.get("allowed_channels", [])]
        set_guild_allowed_channels(guild.id, allowed)

        await interaction.response.send_message(
            "✅ Reloaded allowed-channel configuration from pinned message.",
            ephemeral=True,
        )
