import os, re, random, asyncio, logging, time
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse, parse_qs
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager


import discord
from discord.ext import commands
from discord import app_commands
import aiohttp

from .allowed_channels import (
    init_allowed, allowed_channel_check, slash_allowed_check,
    check_failure_reply, register_allowed_admin_commands
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rbx-gp-bot")

# Try Playwright (guarded)
TRY_PLAYWRIGHT = False
try:
    from playwright.async_api import async_playwright  # type: ignore
    TRY_PLAYWRIGHT = True
except Exception:
    TRY_PLAYWRIGHT = False

# ---------------- Config ----------------
COMMAND_PREFIX = "!"
HEADLESS = True
RENDER_TIMEOUT_MS = 12_000
USER_COOLDOWN_SECONDS = 1.5

FAST_MODE = (os.getenv("FAST_MODE", "1").strip() != "0")
AUTO_SCRAPE_ON_FAIL = (os.getenv("AUTO_SCRAPE_ON_FAIL", "1").strip() != "0")
FORCE_SCRAPE = (os.getenv("FORCE_SCRAPE", "0").strip() == "1")
SPEED_MODE = os.getenv("SPEED_MODE", "fast").strip().lower()  # "fast" | "turbo"
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

FORCE_RP_VIA_COMPARE = (os.getenv("FORCE_RP_VIA_COMPARE", "1").strip() != "0")

THROTTLE_THRESHOLD = int(os.getenv("THROTTLE_THRESHOLD", "11"))
FAST_CONCURRENCY   = int(os.getenv("FAST_CONCURRENCY", "6"))
SLOW_CONCURRENCY   = int(os.getenv("SLOW_CONCURRENCY", "3"))
SLOW_JITTER_MAX    = float(os.getenv("SLOW_JITTER_MAX", "0.35"))
SLOW_BATCH_SLEEP_S = float(os.getenv("SLOW_BATCH_SLEEP_S", "0.8"))

API_RPS = float(os.getenv("API_RPS", "3"))
API_BURST = int(os.getenv("API_BURST", "6"))
RESPECT_RETRY_AFTER = (os.getenv("RESPECT_RETRY_AFTER", "1").strip() != "0")

NOTE_TEXT = "use !help [Version 0.1.1]"
# --- Keep-warm (Render Free) ---
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "").strip()  # e.g., https://your-app.onrender.com/healthz?t=YOUR_TOKEN
KEEPALIVE_INTERVAL_S = int(os.getenv("KEEPALIVE_INTERVAL_S", "240"))  # ping every 4 minutes

# ---- Owner-only guard (/diag) ----
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
def _owner_only(interaction: discord.Interaction) -> bool:
    return OWNER_ID != 0 and interaction.user.id == OWNER_ID

PRICE_PATTERNS = [
    r"\b(\d[\d,\.]*)\s*robux\b",
    r"\brobux\s*(\d[\d,\.]*)\b",
]

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ROBLOSECURITY_MAIN = (os.getenv("ROBLOSECURITY", "") or "").replace("\r", "").replace("\n", "")
ROBLOSECURITY_COOKIES = {
    "A": (os.getenv("ROBLOSECURITY_A", "") or "").replace("\r", "").replace("\n", ""),
    "B": (os.getenv("ROBLOSECURITY_B", "") or "").replace("\r", "").replace("\n", ""),
}
if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN not set in .env")
if not ROBLOSECURITY_MAIN:
    log.warning("ROBLOSECURITY not set — API-only mode. Some prices might fail.")
if not any(ROBLOSECURITY_COOKIES.values()):
    log.warning("ROBLOSECURITY_A / B not set — fewer backup cookies available.")

# ---------------- Bot setup ----------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

_last_used_by_user: Dict[int, float] = {}

# Init allowlist + admin cmds
init_allowed()
register_allowed_admin_commands(bot)

# ---------- aiohttp ----------
_http_session: Optional[aiohttp.ClientSession] = None
@asynccontextmanager
async def http_session():
    global _http_session
    
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "rbx-gp-bot/2.6.1 (+discord)"}
        )
    try:
        yield _http_session
    finally:
        pass

# ---------- rate limit ----------
_api_tokens = API_BURST
_api_last = time.monotonic()
_api_lock = asyncio.Lock()

async def _api_rate_gate():
    """
    Simple token-bucket rate limiter.
    Allows API_BURST instant calls (tokens), refilling at API_RPS per second.
    """
    global _api_tokens, _api_last
    async with _api_lock:
        now = time.monotonic()
        elapsed = now - _api_last
        _api_last = now
        _api_tokens = min(API_BURST, _api_tokens + elapsed * API_RPS)
        if _api_tokens < 1:
            sleep_for = (1 - _api_tokens) / API_RPS
            await asyncio.sleep(sleep_for)
            _api_tokens = 0
        _api_tokens -= 1

RETRY_STATUSES = {429, 500, 502, 503, 504}

async def _http_get_json(url: str, *, cookies: Optional[Dict[str, str]] = None) -> Any:
    """
    GET JSON with retries + simple rate gate.
    """
    await _api_rate_gate()
    if cookies is None and ROBLOSECURITY_MAIN:
        cookies = {".ROBLOSECURITY": ROBLOSECURITY_MAIN}

    async with http_session() as session:
        for attempt in range(5):
            try:
                async with session.get(url, cookies=cookies) as resp:
                    if resp.status in RETRY_STATUSES:
                        if RESPECT_RETRY_AFTER and resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = float(retry_after)
                                    log.warning("429 with Retry-After=%s → sleeping %s", retry_after, delay)
                                    await asyncio.sleep(delay)
                                    continue
                                except Exception:
                                    pass
                        if attempt < 4:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError as e:
                log.warning("HTTP error %s from %s", e.status, url)
                if e.status in RETRY_STATUSES and attempt < 4:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
            except Exception as e:
                log.warning("Error fetching %s: %s", url, e)
                if attempt < 4:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise

# ---------- cache ----------
_cache: Dict[str, Tuple[float, Any]] = {}
def _getc(key: str) -> Optional[Any]:
    ent = _cache.get(key)
    if not ent:
        return None
    ts, val = ent
    if (time.time() - ts) > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return val

def _setc(key: str, val: Any):
    _cache[key] = (time.time(), val)

def _clear_gp_cache():
    keys = [k for k in _cache.keys() if k.startswith("gp:")]
    for k in keys:
        _cache.pop(k, None)
    log.info("Cleared %d cache entries for gamepasses.", len(keys))

# ---------- Roblox API helpers ----------
async def api_get_details(gamepass_id: int) -> Optional[Dict[str, Any]]:
    """
    Call the gamepass details API, with a short cache.
    """
    ck = f"gp:{gamepass_id}"
    cached = _getc(ck)
    if cached is not None:
        return cached
    url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/details"
    data = await _http_get_json(url)
    _setc(ck, data)
    return data

def robux_received_after_fee(price: int) -> int:
    """
    Roblox takes 30% marketplace fee. Round to nearest integer.
    """
    gross = Decimal(price)
    net = gross * Decimal("0.7")
    return int(net.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

async def get_price_via_api(gamepass_id: int) -> Tuple[Optional[int], Dict[str, Any]]:
    """
    Try to get price via Roblox API.
    Returns (price, details).
    price may be None if not found / offsale.
    """
    details = await api_get_details(gamepass_id)
    if not details:
        return None, {}
    pi = details.get("priceInformation") or {}
    price = pi.get("price")
    if price is None:
        return None, details
    try:
        price_int = int(price)
    except Exception:
        price_int = None
    return price_int, details

async def try_cookie(gamepass_id: int, cookie_key: str) -> Optional[int]:
    """
    Try fetching via a backup cookie.
    """
    cookie_val = ROBLOSECURITY_COOKIES.get(cookie_key)
    if not cookie_val:
        return None
    url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/details"
    data = await _http_get_json(url, cookies={".ROBLOSECURITY": cookie_val})
    pi = (data or {}).get("priceInformation") or {}
    price = pi.get("price")
    if price is None:
        return None
    try:
        return int(price)
    except Exception:
        return None

async def get_price_any(gamepass_id: int) -> Tuple[Optional[int], Dict[str, Any], bool]:
    """
    Try main cookie, then backup cookies, then fall back to scraping (if enabled).
    Returns (price, details, used_fallback).
    """
    details = await api_get_details(gamepass_id)
    used_fallback = False
    if details:
        pi = details.get("priceInformation") or {}
        price = pi.get("price")
        if price is not None:
            try:
                return int(price), details, False
            except Exception:
                pass

    # try backup cookies
    for key in ("A", "B"):
        p = await try_cookie(gamepass_id, key)
        if p is not None:
            used_fallback = True
            if not details:
                details = await api_get_details(gamepass_id) or {}
            pi = details.setdefault("priceInformation", {})
            pi["price"] = p
            return p, details, used_fallback

    # scraping could be added here if you want, but for now it's disabled / omitted
    return None, details or {}, used_fallback

def _rp_flag_from_details(details: Dict[str, Any]) -> bool:
    """
    Try to detect whether this gamepass is under regional pricing.
    """
    pi = details.get("priceInformation") or {}
    enabled = [str(x).lower() for x in (pi.get("enabledFeatures") or [])]
    in_exp = bool(pi.get("isInActivePriceOptimizationExperiment"))
    if in_exp: return True
    if any(("regional" in x) or ("price" in x) for x in enabled): return True
    return False

async def get_owner_name(universe_id: int) -> Optional[str]:
    """
    Lookup the root place / game owner name from universe ID.
    """
    url = f"https://games.roblox.com/v1/games/multiget-place-details?universeIds={universe_id}"
    data = await _http_get_json(url)
    if not isinstance(data, list) or not data:
        return None
    owner = (data[0] or {}).get("creator", {}) or {}
    return owner.get("name") or None

# ---------- parsing ----------
def extract_gamepass_id(text: str) -> Optional[int]:
    """
    Extract the first gamepass ID from a URL or numeric input.
    """
    text = text.strip()
    if text.isdigit():
        try:
            return int(text)
        except Exception:
            return None

    try:
        parsed = urlparse(text)
    except Exception:
        return None

    qs = parse_qs(parsed.query or "")
    if "ID" in qs:
        try:
            return int(qs["ID"][0])
        except Exception:
            return None

    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        try:
            return int(parts[-1])
        except Exception:
            return None

    return None

def extract_many_ids(text: str, max_ids: int = 25) -> List[int]:
    """
    Extract multiple possible gamepass IDs from text.
    """
    ids: List[int] = []
    for tok in re.split(r"[^\d]+", text):
        if not tok:
            continue
        try:
            v = int(tok)
        except Exception:
            continue
        if v not in ids:
            ids.append(v)
        if len(ids) >= max_ids:
            break
    return ids

def parse_first_price(text: str) -> Optional[int]:
    """
    Look for a '123 robux' style pattern in text & return the first match.
    """
    lower = text.lower()
    for pat in PRICE_PATTERNS:
        m = re.search(pat, lower)
        if m:
            num = m.group(1).replace(",", "").replace(".", "")
            try:
                return int(num)
            except Exception:
                continue
    return None

# ---------- embeds ----------
def build_min_card(gamepass_id: int, price: Optional[int], details: Dict[str, Any], used_fallback: bool) -> discord.Embed:
    """
    Build a minimal embed for a single gamepass.
    """
    title = details.get("name") or f"Gamepass {gamepass_id}"
    desc = details.get("description") or ""
    pi = details.get("priceInformation") or {}
    rp_flag = _rp_flag_from_details(details)

    extra = []
    if used_fallback:
        extra.append("Used backup cookie")
    if rp_flag:
        extra.append("Possibly regional pricing")

    if price is None:
        price_line = "Price: Unknown / offsale"
    else:
        net = robux_received_after_fee(price)
        price_line = f"Price: **{price}** R$ (you receive ~**{net}** R$)"

    if extra:
        price_line += "\n" + " · ".join(extra)

    embed = discord.Embed(
        title=title,
        url=f"https://www.roblox.com/game-pass/{gamepass_id}",
        description=(desc[:200] + "…") if len(desc) > 200 else desc,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Info", value=price_line, inline=False)

    universe_id = (details.get("universeId") or details.get("universeID") or 0)
    if universe_id:
        embed.set_footer(text=f"Universe ID: {universe_id}")
    else:
        embed.set_footer(text="Universe ID: unknown")

    return embed

def build_summary_card(count: int, with_price: int, offsale: int) -> discord.Embed:
    embed = discord.Embed(
        title="Scan summary",
        description=f"Scanned {count} gamepasses.\nPriced: {with_price}\nOffsale / unknown: {offsale}",
        color=discord.Color.dark_gray(),
    )
    embed.set_footer(text=NOTE_TEXT)
    return embed

# ---------- user throttle ----------
def rate_limit(user_id: int) -> bool:
    now = time.monotonic()
    last = _last_used_by_user.get(user_id)
    if last is not None and (now - last) < USER_COOLDOWN_SECONDS:
        return False
    _last_used_by_user[user_id] = now
    return True

# ---------- background keep-warm ----------
async def _keep_service_warm():
    """
    Periodically ping the external health URL so the Render Free dyno stays warm.
    Uses the shared aiohttp session and swallows errors (never crashes the bot).
    """
    if not KEEPALIVE_URL:
        return
    log.info("Keep-warm enabled → pinging %s every %s seconds", KEEPALIVE_URL, KEEPALIVE_INTERVAL_S)
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            async with http_session() as session:
                async with session.get(KEEPALIVE_URL) as resp:
                    log.info("Keep-warm ping → %s %s", resp.status, await resp.text())
        except Exception as e:
            log.warning("Keep-warm ping failed: %s", e)
        await asyncio.sleep(KEEPALIVE_INTERVAL_S)

# ---------- core scan logic ----------
async def _scan_data(gamepass_id: int) -> Tuple[int, Optional[int], Dict[str, Any], bool]:
    price, details, used_fallback = await get_price_any(gamepass_id)
    return gamepass_id, price, details, used_fallback

async def build_embeds_for_ids(ids: List[int]) -> List[discord.Embed]:
    if SPEED_MODE == "fast":
        sem = asyncio.Semaphore(FAST_CONCURRENCY)
        async def one(i: int):
            async with sem:
                return await _scan_data(i)
        tasks = [one(i) for i in ids]
        results = await asyncio.gather(*tasks)
    else:
        sem = asyncio.Semaphore(SLOW_CONCURRENCY)
        async def one(i: int):
            async with sem:
                await asyncio.sleep(random.random() * SLOW_JITTER_MAX)
                return await _scan_data(i)
        tasks = [one(i) for i in ids]
        results = []
        for i, t in enumerate(tasks):
            results.append(await t)
            if (i + 1) % SLOW_CONCURRENCY == 0:
                await asyncio.sleep(SLOW_BATCH_SLEEP_S)

    embeds: List[discord.Embed] = []
    with_price = 0
    offsale = 0
    for gp_id, price, details, used_fallback in results:
        if price is not None:
            with_price += 1
        else:
            offsale += 1
        embeds.append(build_min_card(gp_id, price, details, used_fallback))

    if len(ids) > 1:
        embeds.append(build_summary_card(len(ids), with_price, offsale))
    return embeds

async def _send(ctx_or_interaction, embeds: List[discord.Embed]):
    """
    Unified send for both prefix + slash.
    """
    if isinstance(ctx_or_interaction, commands.Context):
        # prefix
        for emb in embeds:
            await ctx_or_interaction.reply(embed=emb, mention_author=False)
    else:
        # slash
        if len(embeds) == 1:
            await ctx_or_interaction.response.send_message(embed=embeds[0])
        else:
            await ctx_or_interaction.response.send_message(embeds=[embeds[0]])
            for emb in embeds[1:]:
                await ctx_or_interaction.followup.send(embed=emb)

async def _send_embeds_in_chunks(ctx_or_interaction, embeds: List[discord.Embed], chunk_size: int = 5):
    """
    For scan_multi: send embeds in chunks to avoid hitting limits.
    """
    if isinstance(ctx_or_interaction, commands.Context):
        # prefix
        for i in range(0, len(embeds), chunk_size):
            chunk = embeds[i:i+chunk_size]
            await ctx_or_interaction.reply(embeds=chunk, mention_author=False)
    else:
        # slash
        if not embeds:
            return
        first = embeds[0]
        rest = embeds[1:]
        await ctx_or_interaction.response.send_message(embed=first)
        for i in range(0, len(rest), chunk_size):
            chunk = rest[i:i+chunk_size]
            await ctx_or_interaction.followup.send(embeds=chunk)

# ---------- events ----------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        bot.loop.create_task(_keep_service_warm())
    except Exception as e:
        log.warning("Failed to start keep-warm task: %s", e)
    try:
        await bot.tree.sync()
        log.info("Synced application commands.")
    except Exception as e:
        log.warning("Failed to sync app commands: %s", e)

# ---------- commands ----------
@bot.command(name="ping", help="Check if the bot is alive.")
@allowed_channel_check
async def ping_prefix(ctx: commands.Context):
    if not rate_limit(ctx.author.id):
        return
    await ctx.reply("Pong!", mention_author=False)

@bot.hybrid_command(name="help", with_app_command=True, help="Show help for commands.")
@allowed_channel_check
async def help_prefix(ctx: commands.Context):
    text = (
        "**Commands:**\n"
        f"- `{COMMAND_PREFIX}ping` — basic ping.\n"
        f"- `{COMMAND_PREFIX}scan <id or url>` — scan a single gamepass.\n"
        f"- `{COMMAND_PREFIX}scan_multi <ids or urls>` — scan multiple gamepasses.\n"
        "\n"
        "You can also use slash commands: `/ping`, `/scan`, `/scan_multi`.\n\n"
        f"Note: {NOTE_TEXT}"
    )
    await ctx.reply(text, mention_author=False)

@bot.tree.command(name="ping", description="Check if the bot is alive.")
@slash_allowed_check
async def ping_slash(interaction: discord.Interaction):
    if not rate_limit(interaction.user.id):
        return await interaction.response.send_message("Slow down a bit.", ephemeral=True)
    await interaction.response.send_message("Pong!", ephemeral=True)

@bot.tree.command(name="help", description="Show help for commands.")
@slash_allowed_check
async def help_slash(interaction: discord.Interaction):
    text = (
        "**Commands:**\n"
        f"- `{COMMAND_PREFIX}ping` — basic ping.\n"
        f"- `{COMMAND_PREFIX}scan <id or url>` — scan a single gamepass.\n"
        f"- `{COMMAND_PREFIX}scan_multi <ids or urls>` — scan multiple gamepasses.\n"
        "\n"
        "You can also use these as slash commands.\n\n"
        f"Note: {NOTE_TEXT}"
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.command(name="scan", help="Scan a single gamepass by ID or URL.")
@allowed_channel_check
async def scan_prefix(ctx: commands.Context, *, arg: str):
    if not rate_limit(ctx.author.id):
        return
    gp_id = extract_gamepass_id(arg)
    if not gp_id:
        return await ctx.reply("Could not find a valid gamepass ID in your input.", mention_author=False)
    embeds = await build_embeds_for_ids([gp_id])
    await _send(ctx, embeds)

@bot.tree.command(name="scan", description="Scan a single gamepass by ID or URL.")
@slash_allowed_check
async def scan_slash(interaction: discord.Interaction, arg: str):
    if not rate_limit(interaction.user.id):
        return await interaction.response.send_message("Slow down a bit.", ephemeral=True)
    gp_id = extract_gamepass_id(arg)
    if not gp_id:
        return await interaction.response.send_message("Could not find a valid gamepass ID in your input.", ephemeral=True)
    embeds = await build_embeds_for_ids([gp_id])
    await _send(interaction, embeds)

@bot.command(name="scan_multi", help="Scan multiple gamepasses (IDs or URLs).")
@allowed_channel_check
async def scan_multi_prefix(ctx: commands.Context, *, arg: str):
    if not rate_limit(ctx.author.id):
        return
    ids = extract_many_ids(arg)
    if not ids:
        return await ctx.reply("Could not find any valid gamepass IDs in your input.", mention_author=False)
    embeds = await build_embeds_for_ids(ids)
    await _send_embeds_in_chunks(ctx, embeds)

@bot.tree.command(name="scan_multi", description="Scan multiple gamepasses (IDs or URLs).")
@slash_allowed_check
async def scan_multi_slash(interaction: discord.Interaction, arg: str):
    if not rate_limit(interaction.user.id):
        return await interaction.response.send_message("Slow down a bit.", ephemeral=True)
    ids = extract_many_ids(arg)
    if not ids:
        return await interaction.response.send_message("Could not find any valid gamepass IDs in your input.", ephemeral=True)
    embeds = await build_embeds_for_ids(ids)
    await _send_embeds_in_chunks(interaction, embeds)

@bot.tree.command(name="diag", description="(Owner only) Diagnostic info.")
@app_commands.check(_owner_only)
async def diag_slash(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Cache size: {len(_cache)} entries\n"
        f"API burst: {API_BURST}, RPS: {API_RPS}\n"
        f"Fast mode: {FAST_MODE}, Speed mode: {SPEED_MODE}\n"
        f"Force RP via compare: {FORCE_RP_VIA_COMPARE}\n"
        f"Auto scrape on fail: {AUTO_SCRAPE_ON_FAIL}\n"
        f"Force scrape: {FORCE_SCRAPE}\n"
        f"Cache TTL: {CACHE_TTL_SECONDS}s\n"
        f"Keepalive URL set: {bool(KEEPALIVE_URL)}",
        ephemeral=True,
    )

@diag_slash.error
async def _diag_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if await check_failure_reply(interaction, error): return
    raise error

@help_slash.error
async def _help_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if await check_failure_reply(interaction, error): return
    raise error

@scan_slash.error
async def _scan_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if await check_failure_reply(interaction, error): return
    raise error

@scan_multi_slash.error
async def _scan_multi_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if await check_failure_reply(interaction, error): return
    raise error
