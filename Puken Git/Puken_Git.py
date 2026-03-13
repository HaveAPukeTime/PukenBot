import discord
from discord.ext import commands
import json
import random
import asyncio
import os
import logging
import traceback

# try to support .env files (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Define the bot's prefix and intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needed for on_member_join
bot = commands.Bot(command_prefix='!', intents=intents)

# Global variables for the betting system
BETTING_OPEN = False
CURRENT_MATCH = {}
BETS = {}
STARTING_POINTS = 1000

# Simple shop item definitions (name: (price, effect_value))
SHOP_ITEMS = {
    "diaper_small": {"price": 100, "penalty": 0.10},   # reduces chosen character ratio by 10%
    "diaper_medium": {"price": 250, "penalty": 0.25},  # reduces chosen character ratio by 25%
    "diaper_large": {"price": 500, "penalty": 0.50},   # reduces chosen character ratio by 50%
    "wedding_ring": {"price": 1000, "bonus": 0.5},     # gives chosen user +0.5 to payout ratios
    "soap_shoes": {"price": 300, "protects": ["diaper"], "display_name": "Newt's Soap Shoes"}  # protects a character from diaper effects
}

# ---------------------------
# Registry file helpers
# ---------------------------
REGISTRY_FILE = "character_registry.json"

def load_registry():
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_registry(chars):
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(chars, f, indent=4, ensure_ascii=False)

def parse_select_def(path: str):
    """
    Parse a MUGEN select.def and return a cleaned list of character display names.

    Rules:
    - Find the [Characters] section and read lines until the next [Section] or EOF.
    - Skip "empty", "randomselect" (case-insensitive) and blank lines and comments (lines starting with ';').
    - Clean lines: strip trailing ",, order=..." chunks, strip .def extensions and folder paths.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    # find [Characters] section (case-insensitive)
    start_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("[characters]"):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    chars = []
    for ln in lines[start_idx:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith(";"):
            continue
        # stop if hits another section
        if s.startswith("[") and s.endswith("]"):
            break
        lower = s.lower()
        if lower == "empty" or lower == "randomselect":
            continue

        # remove trailing ",, order=..." or any ",," suffix
        if ",," in s:
            s = s.split(",,", 1)[0].strip()

        # if contains path separators, take last segment
        if "\\" in s or "/" in s:
            s = s.replace("/", "\\")
            s = s.split("\\")[-1].strip()

        # strip .def suffix if present
        if s.lower().endswith(".def"):
            s = s[:-4].strip()

        # final cleanup
        s = s.strip()
        if not s:
            continue

        if s not in chars:
            chars.append(s)
    return chars

# File backed storage for points, win/loss and ring bonuses
def load_points():
    try:
        with open('points.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_points(points):
    with open('points.json', 'w') as f:
        json.dump(points, f, indent=4)

def load_winloss():
    try:
        with open('winloss.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_winloss(winloss):
    with open('winloss.json', 'w') as f:
        json.dump(winloss, f, indent=4)

def load_rings():
    try:
        with open('rings.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_rings(rings):
    with open('rings.json', 'w') as f:
        json.dump(rings, f, indent=4)

# Persistent match history used to compute dynamic prices (each match: user_id -> net_change)
def load_matches():
    try:
        with open('matches.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_matches(matches):
    with open('matches.json', 'w') as f:
        json.dump(matches, f, indent=4)

# Helper to compute average winner gain across last N matches
def avg_gain_per_winner_over_matches(n: int) -> float:
    matches = load_matches()
    if not matches or n <= 0:
        return 0.0
    slice_matches = matches[-n:]
    per_match_avgs = []
    for m in slice_matches:
        # m is expected to be dict mapping user_id -> net_change
        pos = [v for v in m.values() if v > 0]
        if pos:
            per_match_avgs.append(sum(pos) / len(pos))
    if not per_match_avgs:
        return 0.0
    return sum(per_match_avgs) / len(per_match_avgs)

# compute_price used by shop GUI and shop text to avoid NameError
def compute_price(item_key: str) -> int:
    # mapping of how many matches to use per item
    matches_needed = {
        "diaper_small": 10,
        "diaper_medium": 20,
        "diaper_large": 30,
        "wedding_ring": 40,
        "soap_shoes": 15
    }
    base = SHOP_ITEMS.get(item_key, {}).get('price', 0)
    n = matches_needed.get(item_key, 0)
    avg_gain = avg_gain_per_winner_over_matches(n)
    if avg_gain > 0 and n > 0:
        # price ~ average winner gain * n matches; ensure at least base price
        price = int(round(avg_gain * n))
        return max(price, base)
    return base

# Bot's "on ready" event
@bot.event
async def on_ready():
    print(f'{bot.user.name} is online!')

# Event to give new members points
@bot.event
async def on_member_join(member):
    points = load_points()
    if str(member.id) not in points:
        points[str(member.id)] = STARTING_POINTS
        save_points(points)
        print(f'Gave {member.name} {STARTING_POINTS} puken points.')

@bot.command()
async def points(ctx):
    """Checks your current puken points."""
    points_data = load_points()
    user_id = str(ctx.author.id)

    if user_id not in points_data:
        points_data[user_id] = STARTING_POINTS
        save_points(points_data)

    await ctx.send(f'{ctx.author.mention}, you have {points_data[user_id]} puken points.')

@bot.command()
async def leaderboard(ctx):
    """Shows the top 10 richest users."""
    points_data = load_points()

    # Sort users by points in descending order
    sorted_users = sorted(points_data.items(), key=lambda item: item[1], reverse=True)

    leaderboard_msg = "__**Puken Points Leaderboard**__\n"
    for rank, (user_id, balance) in enumerate(sorted_users[:10], 1):
        member = bot.get_user(int(user_id))
        username = member.name if member else "Unknown User"
        leaderboard_msg += f"**{rank}.** {username}: {balance}\n"

    await ctx.send(leaderboard_msg)

# ---------------------------
# Commands to manage registry
# ---------------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def registerchars(ctx, filepath: str = "select.def"):
    """
    Parse a select.def and store characters into the persistent registry JSON.
    Usage: !registerchars path/to/select.def
    If no path is provided the bot will attempt to read ./select.def
    """
    try:
        chars = parse_select_def(filepath)
    expected File "/usr/lib/python3.8/site-packages/discord/ui.py", line 443, in _scheduled_send
        await self.message.edit(view=self)
    except Exception:
        await interaction.response.send_message("Shop closed.", ephemeral=True)

@bot.command()
async def shop(ctx):
    """Show simple text list of shop items (quick)."""
    lines = []
    for key, meta in SHOP_ITEMS.items():
        price = compute_price(key)
        if key.startswith("diaper"):
            lines.append(f"- {key}: {price} pts — reduces odds by {int(meta['penalty']*100)}%")
        elif key == "wedding_ring":
            lines.append(f"- {key}: {price} pts — gives +{meta['bonus']} payout bonus")
        elif key == "soap_shoes":
            lines.append(f"- {key}: {price} pts — protects a character from diapers")
        else:
            lines.append(f"- {key}: {price} pts")
    lines.append("\nBuy with `!buydiaper`, `!buyring @user`, `!buysoap <character>`")
    await ctx.send("__**Shop Items**__\n" + "\n".join(lines))

@bot.command()
async def shopgui(ctx):
    """Open the interactive shop GUI (buttons show details)."""
    embed = discord.Embed(title="Puken Shop", description="Click buttons to view item details.", color=discord.Color.blue())
    embed.add_field(name="diaper_small", value=f"{compute_price('diaper_small')} pts — -10%", inline=False)
    embed.add_field(name="diaper_medium", value=f"{compute_price('diaper_medium')} pts — -25%", inline=False)
    embed.add_field(name="diaper_large", value=f"{compute_price('diaper_large')} pts — -50%", inline=False)
    embed.add_field(name="wedding_ring", value=f"{compute_price('wedding_ring')} pts — +0.5 payout", inline=False)
    embed.add_field(name="soap_shoes", value=f"{compute_price('soap_shoes')} pts — protection", inline=False)
    view = ShopView()
    await ctx.send(embed=embed, view=view)

if __name__ == '__main__':
    # Read token (supports .env via load_dotenv above)
    token = os.environ.get('DISCORD_TOKEN') or os.environ.get('TOKEN')
    if not token:
        logging.error("Discord token not found. Set DISCORD_TOKEN or TOKEN environment variable or add a .env file.")
        raise SystemExit(1)

    logging.info("Discord token found — starting bot.")
    try:
        bot.run(token)
    except Exception:
        logging.exception("bot.run() failed with an exception:")
        raise
