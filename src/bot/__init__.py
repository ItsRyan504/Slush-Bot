import os, re, random, asyncio, logging, time
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse, parse_qs
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

# from keep_alive import keep_alive  # moved to src/main.py
# keep_alive()  # now called in src/main.py

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp

from .allowed_channels import (
    init_allowed, allowed_channel_check, slash_allowed_check,
    check_failure_reply, register_allowed_admin_commands
)

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
AUTO_SCRAPE_ON_FAIL = (os.getenv("AUTO_SCRAPE_ON_FAIL", "0").strip() != "0")
FORCE_SCRAPE = (os.getenv("FORCE_SCRAPE", "0").strip() != "0")
SPEED_MODE = os.getenv("SPEED_MODE", "normal").strip().lower()
FORCE_RP_VIA_COMPARE = (os.getenv("FORCE_RP_VIA_COMPARE", "1").strip() != "0")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
API_RPS = float(os.getenv("API_RPS", "2.5"))
API_BURST = int(os.getenv("API_BURST", "8"))
RESPECT_RETRY_AFTER = (os.getenv("RESPECT_RETRY_AFTER", "1").strip() != "0")

NOTE_TEXT = "use /help [version 0.1.6]"
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

# tokens / cookies
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ROBLOSECURITY_MAIN = (os.getenv("ROBLOSECURITY", "") or "").replace("\r", "").replace("\n", "")
ROBLOSECURITY_COOKIES = {
    "A": (os.getenv("ROBLOSECURITY_A", "") or "").replace("\r", "").replace("\n", ""),
    "B": (os.getenv("ROBLOSECURITY_B", "") or "").replace("\r", "").replace("\n", ""),
}

if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN not set")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rbx-gp-bot")

if not ROBLOSECURITY_MAIN:
    log.warning("ROBLOSECURITY not set — main cookie missing; API calls may fail with 401.")
if not any(ROBLOSECURITY_COOKIES.values()):
    log.info("No extra ROBLOSECURITY_A/ROBLOSECURITY_B cookies configured.")

# Bot intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents,
    help_command=None,
)

_last_used_by_user: Dict[int, float] = {}

# Init allowlist + admin commands
init_allowed()
register_allowed_admin_commands(bot)

# ---------- aiohttp ----------
_http_session: Optional[aiohttp.ClientSession] = None
@asynccontextmanager
async def http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            raise_for_status=False,
        )
    try:
        yield _http_session
    finally:
        pass

async def _keep_service_warm():
    """
    Periodically ping the external health URL so the Render Free dyno stays warm.
    Uses the shared aiohttp session and swallows errors (never crashes the bot).
    """
    if not KEEPALIVE_URL:
        return
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            async with http_session() as sess:
                async with sess.get(KEEPALIVE_URL) as resp:
                    txt = await resp.text()
                    log.info("Keep-warm ping → %s %s", resp.status, txt[:120])
        except Exception as e:
            log.warning("Keep-warm ping failed: %r", e)
        await asyncio.sleep(KEEPALIVE_INTERVAL_S)

# ---------- cache ----------
_cache: Dict[str, Tuple[float, Any]] = {}
def _getc(key: str, *, bypass: bool = False) -> Optional[Any]:
    if bypass: return None
    tup = _cache.get(key)
    if not tup: return None
    ts, val = tup
    if CACHE_TTL_SECONDS <= 0 or time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None); return None
    return val
def _setc(key: str, val: Any): _cache[key] = (time.time(), val)
def _clear_gp_cache(gp_id: str):
    for k in list(_cache.keys()):
        if gp_id in k:
            _cache.pop(k, None)

# ---------- utils ----------
def extract_gamepass_id(text: str) -> Optional[str]:
    if not text: return None
    s = text.strip()
    if "configure?id=" in s.lower():
        try:
            q = parse_qs(urlparse(s).query)
            gid = (q.get("id") or [None])[0]
            if gid and gid.isdigit(): return gid
        except Exception: pass
    m = re.search(r"game[-_]pass/(\d+)", s, flags=re.IGNORECASE)
    if m: return m.group(1)
    m2 = re.search(r"\b(\d{6,20})\b", s)
    if m2: return m2.group(1)
    return None

def parse_first_price(text: str) -> Optional[float]:
    if not text: return None
    low = text.lower()
    for pat in PRICE_PATTERNS:
        m = re.search(pat, low, flags=re.IGNORECASE)
        if m:
            raw = m.group(1)
            for candidate in (raw.replace(",", ""), raw.replace(".", "").replace(",", ".")):
                try: return float(candidate)
                except ValueError: pass
    return None

def round_half_up(n: float) -> int:
    return int(Decimal(n).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def robux_received_after_fee(price: Optional[int]) -> Optional[int]:
    if price is None: return None
    fee = round_half_up(price * 0.30)
    return max(0, int(price) - fee)

def rp_enabled_from_signals(details: Optional[Dict[str, Any]],
                            cross_prices: List[int],
                            default_price: Optional[int]) -> bool:
    if details:
        pi = (details.get("priceInformation") or {})
        enabled = [str(x).lower() for x in (pi.get("enabledFeatures") or [])]
        in_exp = bool(pi.get("isInActivePriceOptimizationExperiment"))
        if in_exp or any(("regional" in x) or ("price" in x) for x in enabled):
            return True
    if len(set(cross_prices)) >= 2: return True
    if default_price is not None and cross_prices:
        if any(p < default_price for p in cross_prices): return True
    return False

# ---------- API rate gate ----------
_api_tokens = API_BURST
_api_last = time.monotonic()
_api_lock = asyncio.Lock()
async def _api_rate_gate():
    global _api_tokens, _api_last
    async with _api_lock:
        now = time.monotonic()
        _api_tokens = min(API_BURST, _api_tokens + (now - _api_last) * API_RPS)
        _api_last = now
        if _api_tokens >= 1:
            _api_tokens -= 1; return
        need = 1 - _api_tokens
        sleep_s = need / API_RPS
    await asyncio.sleep(max(0.01, sleep_s))

# ---------- HTTP JSON ----------
RETRY_STATUSES = {429, 500, 502, 503, 504}
async def _http_get_json(url: str, cookie: Optional[str] = None, *, force: bool = False) -> Optional[Dict[str, Any]]:
    cache_key = f"httpjson::{bool(cookie)}::{url}"
    hit = _getc(cache_key, bypass=force)
    if hit is not None: return hit
    attempts = 3
    cur_cookie = cookie
    for i in range(attempts):
        await _api_rate_gate()
        headers = {}
        if cur_cookie: headers["Cookie"] = f".ROBLOSECURITY={cur_cookie}"
        try:
            async with http_session() as sess:
                async with sess.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning("HTTP %s for %s (cookie=%s)", resp.status, url, bool(cur_cookie))
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        _setc(cache_key, data); return data
                    if resp.status == 401:
                        return None
                    if RESPECT_RETRY_AFTER and resp.status in (429, 503):
                        ra = resp.headers.get("Retry-After")
                        try: delay = float(ra)
                        except Exception: delay = 0.5 * (2 ** i)
                        await asyncio.sleep(min(max(delay, 0.2), 10.0)); continue
                    if resp.status == 403 and cur_cookie:
                        cur_cookie = None; await asyncio.sleep(0.25); continue
                    if resp.status in RETRY_STATUSES:
                        await asyncio.sleep(0.25 * (2 ** i)); continue
        except Exception as e:
            log.warning("HTTP error %r for %s (cookie=%s)", e, url, bool(cur_cookie))
            await asyncio.sleep(0.25 * (2 ** i)); continue
    return None

# ---------- Roblox fetching ----------
async def api_get_details(gp_id: str, cookie: Optional[str], *, force: bool = False) -> Optional[Dict[str, Any]]:
    key = f"details::{cookie is not None}::{gp_id}"
    hit = _getc(key, bypass=force)
    if hit is not None: return hit
    url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gp_id}/details"
    data = await _http_get_json(url, cookie, force=force)
    if data is not None: _setc(key, data)
    return data

async def get_price_via_api(gp_id: str, cookie: Optional[str]) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    data = await api_get_details(gp_id, cookie)
    if not data: return None, None
    price_info = (data.get("priceInformation") or {})
    price = price_info.get("defaultPriceInRobux")
    if price is None: price = data.get("price")
    try: return int(round(float(price))), data
    except Exception: return None, data

async def extract_price_from_page(page) -> Optional[int]:
    await page.wait_for_selector("body", timeout=RENDER_TIMEOUT_MS)
    try:
        el = page.locator('xpath=//div[.//text()[contains(., "Price") or contains(., "price")]]').first
        if await el.count() > 0:
            txt = await el.inner_text()
            p = parse_first_price(txt)
            if p is not None: return int(round(p))
    except Exception: pass
    for sel in [
        '[data-testid="gamepass-price"]',
        '[data-testid="public-item-price"]',
        'xpath=//*[@aria-label and contains(translate(@aria-label,"ROBUX","robux"), "robux")]',
        'xpath=//*[contains(@class,"robux") or contains(@class,"Robux")]',
        'text=/\\bRobux\\b/i',
        'xpath=//div[contains(., "Price")]/following::div[1]',
        'xpath=//span[contains(@class,"text-robux") or contains(@class,"price")][1]',
    ]:
        try:
            txt = await page.locator(sel).first.inner_text(timeout=500)
            p = parse_first_price(txt)
            if p is not None: return int(round(p))
        except Exception: continue
    try:
        p = parse_first_price(await page.inner_text("body"))
        return int(round(p)) if p is not None else None
    except Exception:
        return None

async def get_price_any(gp_id: str, cookie: Optional[str], *, force: bool = False) -> Optional[int]:
    key = f"price_any::{cookie is not None}::{gp_id}"
    hit = _getc(key, bypass=force)
    if hit is not None: return hit

    should_scrape = False
    if not FORCE_SCRAPE:
        cookie_candidates: List[Optional[str]] = []
        if cookie: cookie_candidates.append(cookie)
        for alt in (ROBLOSECURITY_COOKIES.get("A"), ROBLOSECURITY_COOKIES.get("B")):
            if alt and alt not in cookie_candidates: cookie_candidates.append(alt)
        cookie_candidates.append(None)

        for ck in cookie_candidates:
            price, _ = await get_price_via_api(gp_id, ck)
            if price is not None:
                _setc(key, price); return price

        should_scrape = (not FAST_MODE) or (AUTO_SCRAPE_ON_FAIL and TRY_PLAYWRIGHT)
    else:
        should_scrape = True

    if not should_scrape or not TRY_PLAYWRIGHT:
        return None

    url = f"https://www.roblox.com/game-pass/{gp_id}"
    try:
        async with async_playwright() as p:  # type: ignore
            try:
                browser = await p.chromium.launch(headless=HEADLESS)
                ctx = await browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 GP/1.2"),
                    locale="en-US", timezone_id="UTC",
                )
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
                price = await extract_price_from_page(page)
                _setc(key, price)
                return price
            finally:
                await browser.close()
    except Exception as e:
        log.error("Playwright unavailable: %r", e)
        return None

# ---------- Owner ----------
async def get_owner_name(gp_id: str, details_hint: Optional[Dict[str, Any]] = None, *, force: bool = False) -> Optional[str]:
    key = f"owner::{gp_id}"
    hit = _getc(key, bypass=force)
    if hit is not None: return hit
    details = details_hint or \
        (await api_get_details(gp_id, ROBLOSECURITY_MAIN or None, force=force)) or \
        (await api_get_details(gp_id, None, force=force))
    creator = (details or {}).get("creator") or {}
    ctype = (creator.get("type") or (details or {}).get("creatorType") or "").lower()
    if creator.get("name"):
        name = str(creator["name"]).strip()
        if ctype == "user" and not name.startswith("@"): name = f"@{name}"
        _setc(key, name); return name
    if SPEED_MODE == "turbo": return None
    return None

# ---------- Price + RP ----------
async def best_price_and_signals(gp_id: str, *, force: bool = False) -> Tuple[Optional[int], bool, Optional[Dict[str, Any]]]:
    price_main_task = asyncio.create_task(get_price_any(gp_id, ROBLOSECURITY_MAIN or None, force=force))
    details_task    = asyncio.create_task(api_get_details(gp_id, ROBLOSECURITY_MAIN or None, force=force))
    price_main, details = await asyncio.gather(price_main_task, details_task)

    default_price = None
    if details:
        pi = details.get("priceInformation") or {}
        dp = pi.get("defaultPriceInRobux"); dp = dp if dp is not None else details.get("price")
        try: default_price = int(round(float(dp))) if dp is not None else None
        except Exception: default_price = None

    cross_prices: List[int] = []
    if price_main is not None: cross_prices.append(price_main)

    if not FAST_MODE:
        for cookie in (ROBLOSECURITY_COOKIES.get("A"), ROBLOSECURITY_COOKIES.get("B")):
            if cookie:
                p = await get_price_any(gp_id, cookie, force=force)
                if p is not None and p not in cross_prices: cross_prices.append(p)

    price_public: Optional[int] = None
    if FORCE_RP_VIA_COMPARE:
        price_public = await get_price_any(gp_id, None, force=force)
        if price_public is not None and price_main is not None and price_public not in cross_prices:
            cross_prices.append(price_public)

    display_price = price_main if price_main is not None else (default_price if default_price is not None else price_public)
    rp_enabled = rp_enabled_from_signals(details, cross_prices, default_price)
    return display_price, rp_enabled, details

# ---------- Embeds ----------
def build_min_card(price: Optional[int], rp_enabled: bool, owner_name: Optional[str], gp_id: str) -> discord.Embed:
    bar_color = discord.Color.green() if not rp_enabled else discord.Color.red()
    rec = robux_received_after_fee(price)
    price_txt = f"{price} Robux" if price is not None else "—"
    rec_txt   = f"{rec} Robux"   if rec is not None   else "—"
    rp_dot, rp_label = ("🔴", "Disabled") if not rp_enabled else ("🟢", "Enabled")

    e = discord.Embed(title="Gamepass Summary", color=bar_color)
    owner_line = f"*Owner:* {owner_name}\n\n" if owner_name else ""
    e.description = (
        owner_line +
        f"**Gamepass Price** · `{price_txt}`\n"
        f"**You will receive** · `{rec_txt}`\n"
        f"**Regional Pricing** · {rp_dot} **{rp_label}**"
    )
    url = f"https://www.roblox.com/game-pass/{gp_id}"
    e.add_field(name="Gamepass ID", value=f"`{gp_id}`", inline=True)
    e.add_field(name="URL", value=f"[Open Gamepass]({url})", inline=True)
    return e

def build_summary_card(total_price: int, n_scanned: int, n_with_price: int) -> discord.Embed:
    missing = n_scanned - n_with_price
    e = discord.Embed(title="Multi-Scan Summary", color=discord.Color.teal())
    e.description = (
        f"**Total Gamepass Price** · `{total_price} Robux`\n"
        f"**Items scanned** · `{n_scanned}` (with price: `{n_with_price}`, missing: `{missing}`)"
    )
    e.set_footer(text="Note: totals include only entries with a detected price.")
    return e

# ---------- Discord glue ----------
@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    log.info("Mode: FAST=%s AUTO_SCRAPE=%s FORCE_SCRAPE=%s TRY_PW=%s RP_COMPARE=%s",
             FAST_MODE, AUTO_SCRAPE_ON_FAIL, FORCE_SCRAPE, TRY_PLAYWRIGHT, FORCE_RP_VIA_COMPARE)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name=f"{NOTE_TEXT}")
    )
    try:
        synced = await bot.tree.sync()
        log.info("Slash commands synced: %d", len(synced))
    except Exception as e:
        log.warning("Slash sync failed: %s", e)

    # start keep-warm pinger (non-blocking, safe if KEEPALIVE_URL is unset)
    try:
        bot.loop.create_task(_keep_service_warm())
    except Exception as e:
        log.debug("Keep-warm not started: %r", e)

def rate_limit(user_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    last = _last_used_by_user.get(user_id, 0.0)
    if now - last < USER_COOLDOWN_SECONDS: return True
    _last_used_by_user[user_id] = now; return False

def build_help_embed() -> discord.Embed:
    e = discord.Embed(title="Commands", color=discord.Color.blurple())
    e.add_field(name="Prefix",
                value="`!ping`\n`!scan <link-or-id> [--for...ce]`\n`!scan_multi <link-or-id> [more ...] [--force]`\n`!help`",
                inline=False)
    e.add_field(name="Slash",
                value="`/ping`\n`/scan link_or_id:<value> [force:<true|false>]`\n`/scan_multi links:<values> force:<true|false>`\n`/help`",
                inline=False)
    e.add_field(name="Admin (per server)",
                value="`!allow_here`, `!clear_allowed`, `!list_allowed` (also slash).",
                inline=False)
    e.set_footer(text="Tip: paste multiple links/IDs with spaces, commas, or newlines.")
    return e

@allowed_channel_check()
@bot.command(name="help")
async def help_prefix(ctx: commands.Context):
    await ctx.reply(embed=build_help_embed(), mention_author=False)

@bot.tree.command(name="help", description="Help")
@app_commands.check(slash_allowed_check)
async def help_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_help_embed())

# ---------- /diag (owner-only; minimal desc; no unsupported kwargs) ----------
@bot.tree.command(name="diag", description="—")
@app_commands.check(_owner_only)
@app_commands.describe(link_or_id="ID or link")
async def diag_slash(interaction: discord.Interaction, link_or_id: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    gp_id = extract_gamepass_id(link_or_id)
    if not gp_id:
        return await interaction.followup.send("❌ Provide a valid Gamepass link or numeric ID.", ephemeral=True)

    async def try_cookie(label, ck):
        data = await api_get_details(gp_id, ck, force=True)
        ok = bool(
            data and (
                ((data.get("priceInformation") or {}).get("defaultPriceInRobux") is not None)
                or (data.get("price") is not None)
            )
        )
        return f"{label}: {'OK' if ok else '—'}"

    lines = [await try_cookie("main", ROBLOSECURITY_MAIN or None)]
    for k in ("A", "B"):
        ck = ROBLOSECURITY_COOKIES.get(k) or None
        if ck:
            lines.append(await try_cookie(f"cookie_{k}", ck))
    lines.append(await try_cookie("anon", None))
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@diag_slash.error
async def _diag_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try:
            msg = "❌ This command is restricted to the bot owner."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        return
    raise error

# ---------- scan flows ----------
async def _scan_data(gp_id: str, *, force: bool = False) -> Tuple[discord.Embed, Optional[int], Optional[int]]:
    if force: _clear_gp_cache(gp_id)
    price, rp_enabled, details = await best_price_and_signals(gp_id, force=force)
    owner_name = await get_owner_name(gp_id, details_hint=details, force=force)
    embed = build_min_card(price, rp_enabled, owner_name, gp_id)
    receive = robux_received_after_fee(price)
    return embed, price, receive

async def _do_scan(gp_id: str, *, force: bool = False) -> discord.Embed:
    embed, _, _ = await _scan_data(gp_id, force=force); return embed

def extract_many_ids(text: str) -> List[str]:
    if not text: return []
    parts = re.split(r"[,\s]+", text.strip())
    ids: List[str] = []
    for p in parts:
        gid = extract_gamepass_id(p)
        if gid: ids.append(gid)
    seen, uniq = set(), []
    for gid in ids:
        if gid not in seen: uniq.append(gid); seen.add(gid)
    return uniq

async def build_embeds_for_ids(gp_ids: List[str], *, force: bool) -> List[discord.Embed]:
    if not gp_ids: return []
    slow_mode = len(gp_ids) >= 25
    FAST_CONCURRENCY = 8
    SLOW_CONCURRENCY = 4
    THROTTLE_THRESHOLD = 25
    SLOW_BATCH_SLEEP_S = 1.2

    concurrency = SLOW_CONCURRENCY if slow_mode else FAST_CONCURRENCY
    sem = asyncio.Semaphore(concurrency)
    total_price, n_with_price = 0, 0

    async def one(gp_id: str) -> Optional[discord.Embed]:
        async with sem:
            try:
                embed, price, receive = await _scan_data(gp_id, force=force)
                if price is not None:
                    total_price_nonlocal[0] += price
                    n_with_price_nonlocal[0] += 1
                return embed
            except Exception as e:
                log.warning("Error scanning %s: %r", gp_id, e)
                return None

    total_price_nonlocal = [0]
    n_with_price_nonlocal = [0]

    embeds: List[Optional[discord.Embed]] = []
    WAVE = 10
    for i in range(0, len(gp_ids), WAVE):
        batch = gp_ids[i:i+WAVE]
        tasks = [asyncio.create_task(one(g)) for g in batch]
        embeds.extend(await asyncio.gather(*tasks))
        if slow_mode and i + WAVE < len(gp_ids):
            await asyncio.sleep(SLOW_BATCH_SLEEP_S)

    total_price = total_price_nonlocal[0]
    n_with_price = n_with_price_nonlocal[0]
    summary = build_summary_card(total_price, len(gp_ids), n_with_price)
    embeds.append(summary)
    return [e for e in embeds if e]

async def _send_embeds_in_chunks(send_func, embeds: List[discord.Embed]):
    CHUNK = 10
    n = len(embeds)
    if n == 0: return
    if n > CHUNK and n % CHUNK == 1:
        upto = n - CHUNK - 1
        for i in range(0, upto, CHUNK):
            await send_func(embeds=embeds[i:i+CHUNK])
        await send_func(embeds=embeds[upto:upto+CHUNK-1] + [embeds[-1]])
        return
    for i in range(0, n, CHUNK):
        await send_func(embeds=embeds[i:i+CHUNK])

@allowed_channel_check()
@bot.command(name="ping")
async def ping_prefix(ctx: commands.Context):
    await ctx.reply("pong", mention_author=False)

@bot.tree.command(name="ping", description="Ping")
@app_commands.check(slash_allowed_check)
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@allowed_channel_check()
@bot.command(name="scan", help="Scan a Gamepass. Add --force to refresh.")
async def scan_prefix(ctx: commands.Context, *, link_or_id: Optional[str] = None):
    if rate_limit(ctx.author.id): return await ctx.reply("⏳ Slow down a bit and try again.", mention_author=False)
    if not link_or_id: return await ctx.reply("Usage: `!scan <link-or-id> [--force]`", mention_author=False)
    parts = link_or_id.split(); force = False
    if parts and parts[-1].lower() in ("--force","-f"): force, parts = True, parts[:-1]
    gp_id = extract_gamepass_id(" ".join(parts))
    if not gp_id: return await ctx.reply("❌ Please provide a valid game-pass link or numeric ID.", mention_author=False)
    async with ctx.typing():
        embed = await _do_scan(gp_id, force=force)
    await ctx.reply(embed=embed, mention_author=False)

@bot.tree.command(name="scan", description="Scan a Gamepass.")
@app_commands.check(slash_allowed_check)
@app_commands.describe(link_or_id="Gamepass link or ID")
@app_commands.describe(force="If true, bypass cache")
async def scan_slash(interaction: discord.Interaction, link_or_id: str, force: bool = False):
    if rate_limit(interaction.user.id):
        return await interaction.response.send_message("⏳ Slow down a bit and try again.", ephemeral=True)
    gp_id = extract_gamepass_id(link_or_id)
    if not gp_id:
        return await interaction.response.send_message("❌ Please provide a valid game-pass link or numeric ID.", ephemeral=True)
    await interaction.response.defer(thinking=True)
    embed = await _do_scan(gp_id, force=force)
    await interaction.followup.send(embed=embed)

@allowed_channel_check()
@bot.command(name="scan_multi", help="Scan multiple Gamepasses at once.")
async def scan_multi_prefix(ctx: commands.Context, *, links: Optional[str] = None):
    if rate_limit(ctx.author.id): return await ctx.reply("⏳ Slow down a bit and try again.", mention_author=False)
    if not links: return await ctx.reply("Usage: `!scan_multi <link-or-id> [more links/ids ...] [--force]`", mention_author=False)
    parts = links.split(); force = False
    if parts and parts[-1].lower() in ("--force","-f"): force, parts = True, parts[:-1]
    text = " ".join(parts)
    ids = extract_many_ids(text)
    if not ids: return await ctx.reply("❌ Please provide at least one valid Gamepass link or numeric ID.", mention_author=False)
    async with ctx.typing():
        embeds = await build_embeds_for_ids(ids, force=force)
    await _send_embeds_in_chunks(lambda **kw: ctx.reply(**kw, mention_author=False), embeds)

@bot.tree.command(name="scan_multi", description="Scan multiple Gamepasses.")
@app_commands.check(slash_allowed_check)
@app_commands.describe(links="IDs or links separated by space/comma/newlines")
@app_commands.describe(force="If true, bypass cache")
async def scan_multi_slash(interaction: discord.Interaction, links: str, force: bool = False):
    if rate_limit(interaction.user.id):
        return await interaction.response.send_message("⏳ Slow down a bit and try again.", ephemeral=True)
    ids = extract_many_ids(links)
    if not ids:
        return await interaction.response.send_message("❌ Please provide at least one valid Gamepass link or numeric ID.", ephemeral=True)
    await interaction.response.defer(thinking=True)
    embeds = await build_embeds_for_ids(ids, force=force)
    await _send_embeds_in_chunks(lambda **kw: interaction.followup.send(**kw), embeds)

@scan_slash.error
async def _scan_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if await check_failure_reply(interaction, error): return
    raise error

@scan_multi_slash.error
async def _scan_multi_slash_err(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if await check_failure_reply(interaction, error): return
    raise error
