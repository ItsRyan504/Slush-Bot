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
        _allowed_by_guild = {}

def _save_allowed() -> None:
    try:
        serializable = {str(g): list(set(chans)) for g, chans in _allowed_by_guild.items()}
        DATA_FILE.write_text(json.dumps(serializable, indent=2))
    except Exception:
        pass

def init_allowed() -> None:
    _load_allowed()

# ---------- core helpers ----------
def guild_allowed_channel_ids(guild_id: int) -> List[int]:
    return _allowed_by_guild.get(guild_id, [])

def guild_channel_is_allowed(guild_id: int, channel_id: int) -> bool:
    chans = _allowed_by_guild.get(guild_id)
    if not chans:
        return True
    return channel_id in chans

# ---------- decorators ----------
def allowed_channel_check():
    def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True
        if guild_channel_is_allowed(ctx.guild.id, ctx.channel.id):
            return True
        try:
            ctx.reply(
                "❌ This command isn't allowed in this channel. Ask an admin to use `/allow_here`.",
                mention_author=False,
            )
        except Exception:
            pass
        return False
    return commands.check(predicate)

def slash_allowed_check(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    if guild_channel_is_allowed(interaction.guild.id, interaction.channel.id):  # type: ignore[arg-type]
        return True
    raise app_commands.CheckFailure("channel_not_allowed")

async def check_failure_reply(interaction: discord.Interaction, error: app_commands.AppCommandError) -> bool:
    if isinstance(error, app_commands.CheckFailure):
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

# ---------- command registration ----------
def register_allowed_admin_commands(bot: commands.Bot) -> None:
    @bot.command(name="allow_here", help="(Admin) Allow this channel for bot commands in this server.")
    async def allow_here_prefix(ctx: commands.Context):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)
        if not ctx.guild:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        gid, cid = ctx.guild.id, ctx.channel.id
        lst = _allowed_by_guild.get(gid, [])
        if cid not in lst:
            lst.append(cid)
            _allowed_by_guild[gid] = lst
            _save_allowed()
        await ctx.reply(f"✅ Allowed channel set: <#{cid}>", mention_author=False)

    @bot.command(name="clear_allowed", help="(Admin) Clear allowed-channel restrictions for this server.")
    async def clear_allowed_prefix(ctx: commands.Context):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)
        if not ctx.guild:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        _allowed_by_guild.pop(ctx.guild.id, None)
        _save_allowed()
        await ctx.reply("✅ Cleared restrictions — bot allowed in all channels here.", mention_author=False)

    @bot.command(name="list_allowed", help="(Admin) List allowed channels for this server.")
    async def list_allowed_prefix(ctx: commands.Context):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.reply("You need **Manage Server** to use this.", mention_author=False)
        if not ctx.guild:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        gid = ctx.guild.id
        chans = guild_allowed_channel_ids(gid)
        if not chans:
            return await ctx.reply("No restrictions set — bot allowed in all channels.", mention_author=False)
        await ctx.reply("Allowed channels: " + ", ".join(f"<#{c}>" for c in chans), mention_author=False)

    @bot.tree.command(name="allow_here", description="(Admin) Allow this channel for bot commands in this server.")
    async def allow_here_slash(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        gid, cid = interaction.guild.id, interaction.channel.id  # type: ignore[assignment]
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
