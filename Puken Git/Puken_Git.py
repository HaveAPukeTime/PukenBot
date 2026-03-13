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
    except FileNotFoundError:
        await ctx.send(f"select.def not found at `{filepath}`.")
        return

    if not chars:
        await ctx.send("No characters found in the provided select.def (or section missing).")
        return

    save_registry(chars)
    await ctx.send(f"Registered {len(chars)} characters to `{REGISTRY_FILE}`. Use `!showregistry` to view them.")
    class CharacterSelectView(discord.ui.View):
    def __init__(self, registry):
        super().__init__(timeout=None)
        self.registry = registry
        self.selected_a = None
        self.selected_b = None

        # Add dropdowns
        self.add_item(CharacterSelectDropdown(self, "A"))
        self.add_item(CharacterSelectDropdown(self, "B"))
class CharacterSelectDropdown(discord.ui.Select):
    def __init__(self, parent_view, slot):
        self.parent_view = parent_view
        self.slot = slot  # "A" or "B"

        options = [
            discord.SelectOption(label=char, value=char)
            for char in parent_view.registry[:25]  # Discord limit
        ]

        super().__init__(
            placeholder=f"Select Character {slot}",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if self.slot == "A":
            self.parent_view.selected_a = choice
        else:
            self.parent_view.selected_b = choice

        await interaction.response.send_message(
            f"Selected **Character {self.slot}: {choice}**",
            ephemeral=True
        )

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="diaper_small", style=discord.ButtonStyle.secondary, custom_id="shop_diaper_small")
    async def diaper_small_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        price = compute_price("diaper_small")
        text = (
            f"**diaper_small** — {price} points\n"
            "Effect: Reduces a character's odds by 10% (multiplicative).\n"
            "Use: `!buydiaper <character> small`"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="diaper_medium", style=discord.ButtonStyle.secondary, custom_id="shop_diaper_medium")
    async def diaper_medium_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        price = compute_price("diaper_medium")
        text = (
            f"**diaper_medium** — {price} points\n"
            "Effect: Reduces a character's odds by 25% (multiplicative).\n"
            "Use: `!buydiaper <character> medium`"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="diaper_large", style=discord.ButtonStyle.danger, custom_id="shop_diaper_large")
    async def diaper_large_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        price = compute_price("diaper_large")
        text = (
            f"**diaper_large** — {price} points\n"
            "Effect: Reduces a character's odds by 50% (multiplicative).\n"
            "Use: `!buydiaper <character> large`"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="wedding_ring", style=discord.ButtonStyle.primary, custom_id="shop_wedding_ring")
    async def wedding_ring_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        price = compute_price("wedding_ring")
        text = (
            f"**wedding_ring** — {price} points\n"
            "Effect: Gives a user a +0.5 payout bonus. Use `!buyring @user`."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="soap_shoes", style=discord.ButtonStyle.success, custom_id="shop_soap_shoes")
    async def soap_shoes_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        price = compute_price("soap_shoes")
        text = (
            f"**Newt's Soap Shoes** — {price} points\n"
            "Effect: Protects a character from diapers. Use `!buysoap <character>`."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.gray, custom_id="shop_close")
    async def close_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            await interaction.response.edit_message(content="Shop (closed).", embed=None, view=None)
        except Exception:
            try:
                await interaction.response.send_message("Shop closed.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Start Match", style=discord.ButtonStyle.primary, custom_id="start_match_btn")
    async def start_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            # ensure both are selected and not equal
            if not self.selected_a or not self.selected_b:
                await interaction.response.send_message("Please select both characters before starting the match.", ephemeral=True)
                return
            if self.selected_a == self.selected_b:
                await interaction.response.send_message("Please choose two different characters.", ephemeral=True)
                return

            # default ratios (admins can edit later via commands)
            ratio_a = 1.50
            ratio_b = 2.00

            # create match (mirrors betopen logic)
            global BETTING_OPEN, CURRENT_MATCH, BETS
            if BETTING_OPEN:
                await interaction.response.send_message("A betting round is already open. Close it before starting a new one.", ephemeral=True)
                return

            BETTING_OPEN = True
            CURRENT_MATCH = {
                'char_a': self.selected_a,
                'base_ratio_a': ratio_a,
                'ratio_a': ratio_a,
                'char_b': self.selected_b,
                'base_ratio_b': ratio_b,
                'ratio_b': ratio_b,
                'diapers': {},
                'protections': {}
            }
            BETS = {self.selected_a: {}, self.selected_b: {}}

            # Build and send the betting GUI as the interaction response
            try:
                embed = build_betting_embed()
                view = BettingView()
                await interaction.response.send_message(embed=embed, view=view)
                # retrieve the sent message so we can store refs
                try:
                    msg = await interaction.original_response()
                    store_gui_refs(msg, view)
                except Exception as e:
                    print("start_btn: failed to retrieve/store gui message:", e)
            except Exception as e:
                print("start_btn: failed to post betting GUI:", e)
                try:
                    await interaction.followup.send(f"**Betting is now open!**\n**{self.selected_a}** {ratio_a:.2f}x vs **{self.selected_b}** {ratio_b:.2f}x\nUse `!openbetgui` to open the betting GUI.", ephemeral=False)
                except Exception:
                    pass

            # disable selection view (prevent re-use)
            self.stop()
            # remove the selection view from the original message where it was posted
            try:
                await interaction.message.edit(view=None)
            except Exception as e:
                print("start_btn: failed to edit original message to remove view:", e)
        except Exception as e:
            print("start_btn error:", e)
            try:
                await interaction.response.send_message("An internal error occurred starting the match.", ephemeral=True)
            except Exception:
                pass

@bot.command()
@commands.has_permissions(administrator=True)
async def createbetgui(ctx):
    """Open an admin GUI to pick two registered characters and start a match."""
    registry = load_registry()
    if not registry:
        await ctx.send("No characters registered. Use `!registerchars` to import a select.def first.")
        return

    note = ""
    if len(registry) > 25:
        note = "\n\nNote: Discord selects show up to 25 options. Use `!showregistry` if you need a character not in the first 25."

    embed = discord.Embed(
        title="Create Match from Registry",
        description="Pick character A and B from the dropdowns below." + note,
        color=discord.Color.green()
    )
    view = CharacterSelectView(registry)
    await ctx.send(embed=embed, view=view)

@bot.command()
async def shop(ctx):
    """Text list of shop items."""
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
    lines.append("\nBuy with `!buydiaper <character> <small|medium|large>`, `!buyring @user`, `!buysoap <character>`")
    await ctx.send("__**Shop Items**__\n" + "\n".join(lines))

@bot.command()
async def shopgui(ctx):
    """Interactive shop GUI."""
    embed = discord.Embed(title="Puken Shop", description="Click buttons to view item details.", color=discord.Color.blue())
    embed.add_field(name="diaper_small", value=f"{compute_price('diaper_small')} pts — -10%", inline=False)
    embed.add_field(name="diaper_medium", value=f"{compute_price('diaper_medium')} pts — -25%", inline=False)
    embed.add_field(name="diaper_large", value=f"{compute_price('diaper_large')} pts — -50%", inline=False)
    embed.add_field(name="wedding_ring", value=f"{compute_price('wedding_ring')} pts — +0.5 payout", inline=False)
    embed.add_field(name="soap_shoes", value=f"{compute_price('soap_shoes')} pts — protection", inline=False)
    view = ShopView()
    await ctx.send(embed=embed, view=view)

if __name__ == '__main__':
    # Try env first (supports .env via load_dotenv above)
    token = os.environ.get('DISCORD_TOKEN') or os.environ.get('TOKEN')

    # Attempt to locate/load a .env file explicitly and re-check
    try:
        from dotenv import find_dotenv, load_dotenv as _load_dotenv
        env_path = find_dotenv()
        if env_path:
            logging.info(f".env found at: {env_path}")
            # do not override existing env vars; load values missing into os.environ
            _load_dotenv(env_path, override=False)
            token = token or os.environ.get('DISCORD_TOKEN') or os.environ.get('TOKEN')
        else:
            logging.info(".env not found by find_dotenv()")
    except Exception:
        logging.info("python-dotenv not available or failed; relying on environment variables")

    if not token:
        logging.error("Discord token not found in environment or .env.")
        logging.info("Options: create a .env with a line `DISCORD_TOKEN=your_token` or set the DISCORD_TOKEN/TOKEN env var.")
        # Prompt interactively as a fallback (useful for manual runs)
        try:
            token_input = input("Enter bot token (or press Enter to abort): ").strip()
            token = token_input or None
        except Exception:
            token = None

    if not token:
        logging.error("No token provided. Exiting.")
        raise SystemExit(1)

    # safe confirmation (do not print token)
    logging.info(f"Discord token loaded (length: {len(token)}). Starting bot.")
    try:
        bot.run(token)
    except Exception:
        logging.exception("bot.run() failed with an exception:")
        raise
