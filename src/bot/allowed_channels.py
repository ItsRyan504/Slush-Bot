# Per-server channel allowlist with JSON persistence + decorators for prefix/slash.

import os
import json
from pathlib import Path
from typing import Dict, List

import discord
from discord.ext import commands
from discord import app_commands

DATA_FILE = Path(os.getenv("ALLOWED_DATA_FILE", "allowed_channels.json"))
_allowed_by_guild: Dict[int, List[int]] = {}

# ---------- storage ----------
def _load_allowed() -> None:
    global _allowed_by_guild
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            _allowed_by_guild = {int(k): [int(c) for c in v] for k, v in data.items()}
        except Exception:
            _allowed_by_guild = {}
    else:
        _allowed_by_guild = {}

def _save_allowed() -> None:
    try:
        DATA_FILE.write_text(json.dumps(_allowed_by_guild, indent=2, sort_keys=True))
    except Exception:
        pass

def init_allowed() -> None:
    _load_allowed()

# ---------- helpers ----------
def guild_allowed_channel_ids(guild_id: int) -> List[int]:
    return _allowed_by_guild.get(int(guild_id), [])

def is_allowed_here(guild_id: int | None, channel_id: int | None) -> bool:
    if guild_id is None or channel_id is None:
        return True
    allowed = guild_allowed_channel_ids(guild_id)
    return (len(allowed) == 0) or (channel_id in allowed)

def _format_allowed_tags(guild_id: int) -> str:
    chans = guild_allowed_channel_ids(guild_id)
    return ", ".join(f"<#{c}>" for c in chans) if chans else "`(no restriction)`"

# ---------- checks / decorators ----------
def allowed_channel_check():
    async def predicate(ctx: commands.Context):
        gid = getattr(getattr(ctx, "guild", None), "id", None)
        cid = getattr(getattr(ctx, "channel", None), "id", None)
        if is_allowed_here(gid, cid):
            return True
        if gid is not None:
            await ctx.reply(f"❌ This command can only be used in: {_format_allowed_tags(gid)}",
                            mention_author=False)
        return False
    return commands.check(predicate)

def slash_allowed_check(interaction: discord.Interaction) -> bool:
    gid = getattr(getattr(interaction, "guild", None), "id", None)
    cid = getattr(getattr(interaction, "channel", None), "id", None)
    return is_allowed_here(gid, cid)

# ---------- common error handler for slash checks ----------
async def check_failure_reply(interaction: discord.Interaction, error: app_commands.AppCommandError) -> bool:
    if isinstance(error, app_commands.CheckFailure):
        gid = getattr(getattr(interaction, "guild", None), "id", None)
        msg = "❌ This command is restricted in this server."
        if gid is not None:
            msg = f"❌ This command can only be used in: {_format_allowed_tags(gid)}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        return True
    return False

# ---------- admin commands (prefix + slash) ----------
def register_allowed_admin_commands(bot: commands.Bot) -> None:
    # prefix
    @bot.command(name="allow_here", help="(Admin) Allow this channel for bot commands in this server.")
    async def allow_here_prefix(ctx: commands.Context):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)
        gid, cid = ctx.guild.id, ctx.channel.id
        lst = _allowed_by_guild.get(gid, [])
        if cid not in lst:
            lst.append(cid)
            _allowed_by_guild[gid] = lst
            _save_allowed()
        await ctx.reply(f"✅ Allowed channel set: <#{cid}>", mention_author=False)

    @bot.command(name="clear_allowed", help="(Admin) Clear all allowed-channel restrictions for this server.")
    async def clear_allowed_prefix(ctx: commands.Context):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)
        _allowed_by_guild.pop(ctx.guild.id, None)
        _save_allowed()
        await ctx.reply("✅ Cleared restrictions — bot allowed in all channels here.", mention_author=False)

    @bot.command(name="list_allowed", help="(Admin) List allowed channels for this server.")
    async def list_allowed_prefix(ctx: commands.Context):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)
        gid = ctx.guild.id
        chans = guild_allowed_channel_ids(gid)
        if not chans:
            return await ctx.reply("No restrictions set — bot allowed in all channels.", mention_author=False)
        await ctx.reply("Allowed channels: " + ", ".join(f"<#{c}>" for c in chans), mention_author=False)

    # slash
    @bot.tree.command(name="allow_here", description="(Admin) Allow this channel for bot commands in this server.")
    async def allow_here_slash(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        gid, cid = interaction.guild.id, interaction.channel.id
        lst = _allowed_by_guild.get(gid, [])
        if cid not in lst:
            lst.append(cid)
            _allowed_by_guild[gid] = lst
            _save_allowed()
        await interaction.response.send_message(f"✅ Allowed channel set: <#{cid}>", ephemeral=True)

    @bot.tree.command(name="clear_allowed", description="(Admin) Clear allowed-channel restrictions for this server.")
    async def clear_allowed_slash(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        _allowed_by_guild.pop(interaction.guild.id, None)
        _save_allowed()
        await interaction.response.send_message("✅ Cleared restrictions — bot allowed in all channels here.", ephemeral=True)

    @bot.tree.command(name="list_allowed", description="(Admin) List allowed channels for this server.")
    async def list_allowed_slash(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        chans = guild_allowed_channel_ids(interaction.guild.id)
        if not chans:
            return await interaction.response.send_message("No restrictions set — bot allowed in all channels.", ephemeral=True)
        await interaction.response.send_message("Allowed channels: " + ", ".join(f"<#{c}>" for c in chans), ephemeral=True)
