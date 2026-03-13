import discord
from discord.ext import commands
import json
import random
import asyncio
import os

# try to support .env files (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

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

@bot.command()
async def showregistry(ctx, limit: int = 50):
    """Show registered characters (up to `limit`)."""
    chars = load_registry()
    if not chars:
        await ctx.send("No characters registered. Use `!registerchars` to import a select.def.")
        return
    display = "\n".join(f"{i+1}. {name}" for i, name in enumerate(chars[:limit]))
    await ctx.send(f"**Registered characters (showing {min(limit, len(chars))} of {len(chars)}):**\n{display}")

# ---------------------------
# GUI to pick two characters and start a match
# ---------------------------
class CharacterSelectView(discord.ui.View):
    def __init__(self, registry):
        super().__init__(timeout=120)
        # limit to first 25 choices (discord select limit)
        options = [discord.SelectOption(label=name, value=name) for name in registry[:25]]
        self.select_a = discord.ui.Select(placeholder="Choose character A", min_values=1, max_values=1, options=options, custom_id="char_select_a")
        self.select_b = discord.ui.Select(placeholder="Choose character B", min_values=1, max_values=1, options=options, custom_id="char_select_b")
        self.add_item(self.select_a)
        self.add_item(self.select_b)
        self.selected_a = None
        self.selected_b = None

        async def select_a_callback(interaction: discord.Interaction):
            self.selected_a = self.select_a.values[0]
            await interaction.response.send_message(f"Selected A: {self.selected_a}", ephemeral=True)

        async def select_b_callback(interaction: discord.Interaction):
            self.selected_b = self.select_b.values[0]
            await interaction.response.send_message(f"Selected B: {self.selected_b}", ephemeral=True)

        self.select_a.callback = select_a_callback
        self.select_b.callback = select_b_callback

    @discord.ui.button(label="Start Match", style=discord.ButtonStyle.primary, custom_id="start_match_btn")
    async def start_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
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
        await interaction.response.send_message(f"**Betting is now open!**\n**{self.selected_a}** has odds of **{ratio_a:.2f}x**\n**{self.selected_b}** has odds of **{ratio_b:.2f}x**\nUse `!bet [character] [amount]` to place your bet. Use `!shop` to view purchasable items.", ephemeral=False)
        # disable view (prevent re-use)
        self.stop()
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

@bot.command()
@commands.has_permissions(administrator=True)
async def createbetgui(ctx):
    """Open an embed GUI for admins to pick two registered characters and start a match."""
    registry = load_registry()
    if not registry:
        await ctx.send("No characters registered. Use `!registerchars` to import a select.def first.")
        return

    # warn if registry is larger than 25 (select will show only first 25)
    note = ""
    if len(registry) > 25:
        note = "\n\nNote: Discord selects can show up to 25 options. Use `!showregistry` if you need to find a character not in the first 25."

    embed = discord.Embed(title="Create Match from Registry", description="Pick character A and B from the dropdowns below." + note, color=discord.Color.green())
    view = CharacterSelectView(registry)
    await ctx.send(embed=embed, view=view)

# ---------------------------
# (existing commands continue below)
# ---------------------------

@bot.command()
@commands.has_permissions(administrator=True)
async def betopen(ctx, character_a: str, ratio_a: float, character_b: str, ratio_b: float):
    """Starts a new betting round for two MUGEN characters with given ratios.
    Usage: !betopen character_a 1.5 character_b 2.0"""
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if BETTING_OPEN:
        await ctx.send("Betting is already open! Please close the current match first.")
        return

    BETTING_OPEN = True
    # store base ratios so diapers can modify current ratios but still reference originals
    CURRENT_MATCH = {
        'char_a': character_a,
        'base_ratio_a': ratio_a,
        'ratio_a': ratio_a,
        'char_b': character_b,
        'base_ratio_b': ratio_b,
        'ratio_b': ratio_b,
        'diapers': {}  # mapping character -> list of applied diaper dicts
    }
    BETS = {character_a: {}, character_b: {}}

    await ctx.send(f"**Betting is now open!**\n"
                   f"**{character_a}** has odds of **{ratio_a:.2f}x**\n"
                   f"**{character_b}** has odds of **{ratio_b:.2f}x**\n"
                   f"Use `!bet [character] [amount]` to place your bet. Use `!shop` to view purchasable items.")

@bot.command()
@commands.has_permissions(administrator=True)
async def betclose(ctx):
    """Closes the current betting round.""" 
    global BETTING_OPEN

    if not BETTING_OPEN:
        await ctx.send("There is no active bet to close.")
        return

    BETTING_OPEN = False
    await ctx.send("Betting is now **closed**! No more bets can be placed.")

@bot.command()
@commands.has_permissions(administrator=True)
async def payout(ctx, winner_name: str):
    """Pays out points to the winners of the match.""" 
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if BETTING_OPEN:
        await ctx.send("Please close the betting with `!betclose` before declaring a winner.")
        return

    if not CURRENT_MATCH:
        await ctx.send("No match has been set up to pay out.")
        return

    points = load_points()
    total_paid_out = 0

    # --- Win/Loss tracking ---
    winloss = load_winloss()
    char_a = CURRENT_MATCH['char_a']
    char_b = CURRENT_MATCH['char_b']
    # Initialize if not present
    if char_a not in winloss:
        winloss[char_a] = {"wins": 0, "losses": 0}
    if char_b not in winloss:
        winloss[char_b] = {"wins": 0, "losses": 0}

    # Determine the winning ratio
    winning_ratio = None
    if winner_name.lower() == char_a.lower():
        winning_ratio = CURRENT_MATCH['ratio_a']
        winners = BETS.get(char_a, {})
        winloss[char_a]["wins"] += 1
        winloss[char_b]["losses"] += 1
    elif winner_name.lower() == char_b.lower():
        winning_ratio = CURRENT_MATCH['ratio_b']
        winners = BETS.get(char_b, {})
        winloss[char_b]["wins"] += 1
        winloss[char_a]["losses"] += 1
    else:
        await ctx.send(f"Invalid winner. Please enter `{char_a}` or `{char_b}`.")
        return

    # Prepare match record for history (user_id -> net change)
    match_record = {}

    # Load ring bonuses
    rings = load_rings()

    # Pay out the winners — include ring bonuses per-user if present
    for user_id, amount in winners.items():
        bonus = float(rings.get(user_id, 0))
        total_ratio = winning_ratio + bonus
        winnings = int(amount * total_ratio)
        points[user_id] = points.get(user_id, 0) + winnings
        total_paid_out += winnings
        match_record[user_id] = match_record.get(user_id, 0) + winnings

    # Subtract points from losers
    losers = {}
    if winner_name.lower() == char_a.lower():
        losers = BETS.get(char_b, {})
    else:
        losers = BETS.get(char_a, {})

    for user_id, amount in losers.items():
        current_points = points.get(user_id, 0)
        new_balance = current_points - amount
        points[user_id] = max(0, new_balance) # This line prevents the balance from going below 0
        match_record[user_id] = match_record.get(user_id, 0) - amount

    save_points(points)
    save_winloss(winloss)  # Save win/loss data

    # Append match_record to persistent history used for pricing
    matches = load_matches()
    matches.append(match_record)
    # keep history bounded
    if len(matches) > 1000:
        matches = matches[-1000:]
    save_matches(matches)

    await ctx.send(f"**{winner_name}** wins! **{len(winners)}** people won a total of **{total_paid_out}** puken points! 🥳")

    # Reset the match state
    CURRENT_MATCH = {}
    BETTING_OPEN = False
    BETS = {}

@bot.command()
async def bet(ctx, character: str, amount: int):
    """Places a bet on a character.""" 
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if not BETTING_OPEN:
        await ctx.send("Betting is not currently open.")
        return

    if amount <= 0:
        await ctx.send("You can't bet a negative or zero amount.")
        return

    user_id = str(ctx.author.id)
    points = load_points()

    if user_id not in points:
        points[user_id] = STARTING_POINTS
        save_points(points)

    if points[user_id] < amount:
        await ctx.send(f"You don't have enough points. You only have {points[user_id]}.")
        return

    # Check if the character is a valid option
    if character.lower() not in [CURRENT_MATCH['char_a'].lower(), CURRENT_MATCH['char_b'].lower()]:
        await ctx.send(f"Invalid character. Please bet on `{CURRENT_MATCH['char_a']}` or `{CURRENT_MATCH['char_b']}`.")
        return

    # Check if the user has already bet
    if user_id in BETS.get(CURRENT_MATCH['char_a'], {}) or user_id in BETS.get(CURRENT_MATCH['char_b'], {}):
        await ctx.send("You have already placed a bet for this match.")
        return

    # Store the bet
    if character.lower() == CURRENT_MATCH['char_a'].lower():
        BET_KEY = CURRENT_MATCH['char_a']
    else:
        BET_KEY = CURRENT_MATCH['char_b']
            
    BETS[BET_KEY][user_id] = amount

    # Announce ring usage if user has ring bonus
    rings = load_rings()
    if user_id in rings and float(rings[user_id]) > 0:
        await ctx.send("adding littles up!")  # required phrase when ring-bonus is used

    await ctx.send(f'{ctx.author.mention} placed a bet of **{amount}** on **{character}**.')

@bot.command()
async def betall(ctx, character: str):
    """Bets your entire balance on a character.""" 
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if not BETTING_OPEN:
        await ctx.send("Betting is not currently open.")
        return

    user_id = str(ctx.author.id)
    points = load_points()

    if user_id not in points:
        points[user_id] = STARTING_POINTS
        save_points(points)

    amount = points[user_id]

    if amount <= 0:
        await ctx.send("You don't have any points to bet.")
        return

    # Check if the character is a valid option
    if character.lower() not in [CURRENT_MATCH['char_a'].lower(), CURRENT_MATCH['char_b'].lower()]:
        await ctx.send(f"Invalid character. Please bet on `{CURRENT_MATCH['char_a']}` or `{CURRENT_MATCH['char_b']}`.")
        return

    # Check if the user has already bet
    if user_id in BETS.get(CURRENT_MATCH['char_a'], {}) or user_id in BETS.get(CURRENT_MATCH['char_b'], {}):
        await ctx.send("You have already placed a bet for this match.")
        return

    # Store the bet
    if character.lower() == CURRENT_MATCH['char_a'].lower():
        BET_KEY = CURRENT_MATCH['char_a']
    else:
        BET_KEY = CURRENT_MATCH['char_b']

    BETS[BET_KEY][user_id] = amount

    # Announce ring usage if user has ring bonus
    rings = load_rings()
    if user_id in rings and float(rings[user_id]) > 0:
        await ctx.send("adding littles up!")  # required phrase when ring-bonus is used

    await ctx.send(f'{ctx.author.mention} placed a bet of **{amount}** on **{character}**.')

@bot.command()
@commands.has_permissions(administrator=True)
async def betsummary(ctx):
    """Shows a summary of all current bets.""" 
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if not CURRENT_MATCH:
        await ctx.send("There is no active match to view bets for.")
        return

    message = "**Current Bets Summary**\n\n"
    
    char_a = CURRENT_MATCH['char_a']
    char_b = CURRENT_MATCH['char_b']

    # Summary for the first character
    bets_a = BETS.get(char_a, {})
    total_a = sum(bets_a.values())
    message += f"**{char_a}** (Total Bets: {total_a} puken points) — Current Odds: {CURRENT_MATCH.get('ratio_a'):.2f}x\n"
    if bets_a:
        for user_id, amount in bets_a.items():
            user = bot.get_user(int(user_id))
            username = user.name if user else "Unknown User"
            message += f"- {username}: {amount}\n"
    else:
        message += "- No bets on this character yet.\n"
    
    message += "\n"

    # Summary for the second character
    bets_b = BETS.get(char_b, {})
    total_b = sum(bets_b.values())
    message += f"**{char_b}** (Total Bets: {total_b} puken points) — Current Odds: {CURRENT_MATCH.get('ratio_b'):.2f}x\n"
    if bets_b:
        for user_id, amount in bets_b.items():
            user = bot.get_user(int(user_id))
            username = user.name if user else "Unknown User"
            message += f"- {username}: {amount}\n"
    else:
        message += "- No bets on this character yet.\n"

    # Show applied diapers if any
    diapers = CURRENT_MATCH.get('diapers', {})
    if diapers:
        message += "\n**Applied Diapers:**\n"
        for char, applied in diapers.items():
            message += f"- {char}:\n"
            for item in applied:
                message += f"  * {item['name']} (-{int(item['penalty']*100)}%) bought by <@{item['buyer']}> for {item['price']} points\n"

    # Show protections (e.g. Newt's Soap Shoes) if any
    protections = CURRENT_MATCH.get('protections', {})
    if protections:
        message += "\n**Protections (immunities):**\n"
        for char, applied in protections.items():
            message += f"- {char}:\n"
            for item in applied:
                display = SHOP_ITEMS.get(item['name'], {}).get('display_name', item['name'])
                message += f"  * {display} bought by <@{item['buyer']}> for {item['price']} points\n"

    await ctx.send(message)

@bot.command()
@commands.has_permissions(administrator=True)
async def resetpoints(ctx, member: discord.Member):
    """Resets a single user's points to the starting amount.""" 
    points = load_points()
    user_id = str(member.id)

    points[user_id] = STARTING_POINTS
    save_points(points)

    await ctx.send(f"Reset {member.mention}'s points to {STARTING_POINTS}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def resetall(ctx):
    """Resets all users' points to the starting amount.""" 
    # Add a confirmation prompt to prevent accidental use
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['yes', 'y']

    await ctx.send("Are you sure you want to reset all user balances? This cannot be undone. Type 'yes' to confirm.")
    
    try:
        msg = await bot.wait_for('message', check=check, timeout=30.0)
    except asyncio.TimeoutError:
        await ctx.send("Reset command timed out. All balances remain unchanged.")
        return

    # If confirmed, reset all points
    points = {}
    save_points(points)

    await ctx.send("All user balances have been reset to 0. New users will start with 1000 points.")

@bot.command()
@commands.has_permissions(administrator=True)
async def seepoints(ctx, member: discord.Member):
    """Shows the mentioned user's puken points balance.""" 
    points_data = load_points()
    user_id = str(member.id)

    # If the user doesn't have points yet, show starting points
    balance = points_data.get(user_id, STARTING_POINTS)
    await ctx.send(f"{member.mention} has {balance} puken points.")
    
@bot.command()
async def winloss(ctx, *, character: str):
    """Shows the win/loss record for a character.""" 
    winloss = load_winloss()
    char_stats = winloss.get(character, {"wins": 0, "losses": 0})
    await ctx.send(f"**{character}** — Wins: {char_stats['wins']}, Losses: {char_stats['losses']}")

@bot.command()
async def betrandom(ctx, amount: int):
    """Bets the specified amount on a random character in the current match.""" 
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if not BETTING_OPEN:
        await ctx.send("Betting is not currently open.")
        return

    if amount <= 0:
        await ctx.send("You can't bet a negative or zero amount.")
        return

    user_id = str(ctx.author.id)
    points = load_points()

    if user_id not in points:
        points[user_id] = STARTING_POINTS
        save_points(points)

    if points[user_id] < amount:
        await ctx.send(f"You don't have enough points. You only have {points[user_id]}.")
        return

    # Check if the user has already bet
    if user_id in BETS.get(CURRENT_MATCH['char_a'], {}) or user_id in BETS.get(CURRENT_MATCH['char_b'], {}):
        await ctx.send("You have already placed a bet for this match.")
        return

    # Randomly select a character
    characters = [CURRENT_MATCH['char_a'], CURRENT_MATCH['char_b']]
    chosen_character = random.choice(characters)

    # Store the bet
    BETS[chosen_character][user_id] = amount

    # Announce ring usage if user has ring bonus
    rings = load_rings()
    if user_id in rings and float(rings[user_id]) > 0:
        await ctx.send("adding littles up!")  # required phrase when ring-bonus is used

    await ctx.send(f"{ctx.author.mention} placed a bet of **{amount}** on **{chosen_character}** (randomly selected).")
         
@bot.command()
async def editbet(ctx, character: str, amount: int):
    """Edit your bet before betting closes. Usage: !editbet <character> <amount>""" 
    global BETTING_OPEN, CURRENT_MATCH, BETS

    if not BETTING_OPEN:
        await ctx.send("Betting is not currently open.")
        return

    if amount <= 0:
        await ctx.send("You can't bet a negative or zero amount.")
        return

    user_id = str(ctx.author.id)
    points = load_points()

    if user_id not in points:
        points[user_id] = STARTING_POINTS
        save_points(points)

    if points[user_id] < amount:
        await ctx.send(f"You don't have enough points. You only have {points[user_id]}.")
        return

    # Validate character
    char_a = CURRENT_MATCH.get('char_a')
    char_b = CURRENT_MATCH.get('char_b')
    if character.lower() not in [char_a.lower(), char_b.lower()]:
        await ctx.send(f"Invalid character. Please bet on `{char_a}` or `{char_b}`.")
        return

    # Remove previous bet if it exists
    bet_found = False
    for char in [char_a, char_b]:
        if user_id in BETS.get(char, {}):
            del BETS[char][user_id]
            bet_found = True

    if not bet_found:
        await ctx.send("You don't have a bet to edit. Use !bet or !betall to place one.")
        return

    # Place the new bet
    if character.lower() == char_a.lower():
        BET_KEY = char_a
    else:
        BET_KEY = char_b

    BETS[BET_KEY][user_id] = amount

    # Announce ring usage if user has ring bonus
    rings = load_rings()
    if user_id in rings and float(rings[user_id]) > 0:
        await ctx.send("adding littles up!")  # required phrase when ring-bonus is used

    await ctx.send(f"{ctx.author.mention}, your bet has been updated to **{amount}** on **{character}**.")


# Shop commands: list and purchases
@bot.command()
async def shop(ctx):
    """Displays items available for purchase."""
    msg = "**Shop Items**\n"
    msg += "- diaper_small: 100 points (reduces a character's odds by 10%)\n"
    msg += "- diaper_medium: 250 points (reduces a character's odds by 25%)\n"
    msg += "- diaper_large: 500 points (reduces a character's odds by 50%)\n"
    msg += "- wedding_ring: 1000 points (give a user +0.5 bonus to payouts)\n"
    msg += "- soap_shoes: 300 points (Newt's Soap Shoes — protects a character from diapers; says \"uh meow?\" when bought)\n"
    msg += "\nUsage: `!buydiaper <character> <small|medium|large>`, `!buyring @user` or `!buysoap <character>`\n"
    await ctx.send(msg)

# --- GUI for the shop using Discord UI components ---
class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="diaper_small", style=discord.ButtonStyle.secondary, custom_id="shop_diaper_small")
    async def diaper_small_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        text = (
            "**diaper_small** — 100 points\n"
            "Effect: Reduces a character's odds by 10% (multiplicative).\n"
            "How to use: While a bet is open, use `!buydiaper <character> small` to apply.\n"
            "Notes: The reduction is multiplicative and recorded on the match; ratio won't drop below 0.1x."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="diaper_medium", style=discord.ButtonStyle.secondary, custom_id="shop_diaper_medium")
    async def diaper_medium_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        text = (
            "**diaper_medium** — 250 points\n"
            "Effect: Reduces a character's odds by 25% (multiplicative).\n"
            "How to use: While a bet is open, use `!buydiaper <character> medium` to apply.\n"
            "Notes: Stacks with other diapers and updates the current odds immediately."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="diaper_large", style=discord.ButtonStyle.danger, custom_id="shop_diaper_large")
    async def diaper_large_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        text = (
            "**diaper_large** — 500 points\n"
            "Effect: Reduces a character's odds by 50% (multiplicative).\n"
            "How to use: While a bet is open, use `!buydiaper <character> large` to apply.\n"
            "Notes: This is the strongest diaper; use wisely."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="wedding_ring", style=discord.ButtonStyle.primary, custom_id="shop_wedding_ring")
    async def wedding_ring_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        text = (
            "**wedding_ring** — 1000 points\n"
            "Effect: Gives a user a +0.5 payout bonus (added to the winning ratio) when they win bets.\n"
            "How to use: Buy and give to a user with `!buyring @user`.\n"
            "Notes: The bot will send `adding littles up!` when a user with a ring places a bet. Bonuses stack if multiple rings are given."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="soap_shoes", style=discord.ButtonStyle.success, custom_id="shop_soap_shoes")
    async def soap_shoes_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        text = (
            "**Newt's Soap Shoes** — 300 points\n"
            "Effect: Protects a character from diapers for the remainder of the match and removes already-applied diapers for that character.\n"
            "How to use: While a bet is open, use `!buysoap <character>` to apply. The bot will say `uh meow?` when bought.\n"
            "Notes: Protections prevent future diaper purchases targeting that character."
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.gray, custom_id="shop_close")
    async def close_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            await interaction.message.edit(content="Shop (closed).", embed=None, view=None)
            await interaction.response.send_message("Shop closed.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Shop closed.", ephemeral=True)

@bot.command()
async def shopgui(ctx):
    """Opens an interactive shop GUI that explains items and prices."""
    embed = discord.Embed(
        title="Puken Shop",
        description="Click any button to see detailed info about an item. Buy items using the text commands shown below.",
        color=discord.Color.blue()
    )
    # compute_price is defined above, so this will no longer raise NameError
    embed.add_field(name="diaper_small", value=f"{compute_price('diaper_small')} points — reduces odds by 10%", inline=False)
    embed.add_field(name="diaper_medium", value=f"{compute_price('diaper_medium')} points — reduces odds by 25%", inline=False)
    embed.add_field(name="diaper_large", value=f"{compute_price('diaper_large')} points — reduces odds by 50%", inline=False)
    embed.add_field(name="wedding_ring", value=f"{compute_price('wedding_ring')} points — gives a +0.5 payout bonus to a user", inline=False)
    embed.add_field(name="soap_shoes", value=f"{compute_price('soap_shoes')} points — Newt's Soap Shoes (protects from diapers)", inline=False)
    embed.add_field(name="Buy commands", value="`!buydiaper <character> <small|medium|large>`\n`!buyring @user`\n`!buysoap <character>`", inline=False)

    view = ShopView()
    await ctx.send(embed=embed, view=view)

@bot.command()
async def buydiaper(ctx, character: str, size: str):
    """Buy a diaper to reduce the payout ratio of a character in the currently open bet.
    Usage: !buydiaper <character> <small|medium|large>"""
    global CURRENT_MATCH, BETTING_OPEN

    if not BETTING_OPEN or not CURRENT_MATCH:
        await ctx.send("No active betting match to target. Open a match with `!betopen` first.")
        return

    size_key = size.lower()
    size_map = {
        "small": "diaper_small",
        "medium": "diaper_medium",
        "large": "diaper_large"
    }
    if size_key not in size_map:
        await ctx.send("Invalid size. Choose small, medium, or large.")
        return

    item_key = size_map[size_key]
    item = SHOP_ITEMS[item_key]
    # use computed dynamic price if available
    price = compute_price(item_key)
    penalty = item["penalty"]

    buyer_id = str(ctx.author.id)
    points = load_points()
    if buyer_id not in points:
        points[buyer_id] = STARTING_POINTS
        save_points(points)

    target_char = None
    if character.lower() == CURRENT_MATCH['char_a'].lower():
        target_char = CURRENT_MATCH['char_a']
        ratio_key = 'ratio_a'
    elif character.lower() == CURRENT_MATCH['char_b'].lower():
        target_char = CURRENT_MATCH['char_b']
        ratio_key = 'ratio_b'
    else:
        await ctx.send(f"Invalid character. Please pick `{CURRENT_MATCH['char_a']}` or `{CURRENT_MATCH['char_b']}`.")
        return

    # Check for protections (e.g. Newt's Soap Shoes)
    protections = CURRENT_MATCH.get('protections', {})
    if target_char in protections and protections[target_char]:
        await ctx.send(f"You cannot diaper {target_char}; they are protected (Newt's Soap Shoes).")
        return

    if points[buyer_id] < price:
        await ctx.send(f"You don't have enough points to buy this diaper. You need {price} points.")
        return

    # Deduct points from buyer
    points[buyer_id] -= price
    save_points(points)

    # Apply penalty multiplicatively to the current ratio
    old_ratio = CURRENT_MATCH[ratio_key]
    new_ratio = max(0.1, old_ratio * (1 - penalty))  # ensure ratio doesn't drop below 0.1
    CURRENT_MATCH[ratio_key] = new_ratio

    # Record diaper application
    diapers = CURRENT_MATCH.get('diapers', {})
    if target_char not in diapers:
        diapers[target_char] = []
    diapers[target_char].append({
        "name": item_key,
        "price": price,
        "penalty": penalty,
        "buyer": buyer_id
    })
    CURRENT_MATCH['diapers'] = diapers

    await ctx.send(f"{ctx.author.mention} bought a **{size_key} diaper** for **{target_char}** for {price} points. {target_char}'s odds changed {old_ratio:.2f}x -> {new_ratio:.2f}x.")

@bot.command()
async def buysoap(ctx, character: str):
    """Buy Newt's Soap Shoes to protect a character from diapers and remove already-applied diapers.
    Usage: !buysoap <character>"""
    global CURRENT_MATCH, BETTING_OPEN

    if not BETTING_OPEN or not CURRENT_MATCH:
        await ctx.send("No active betting match to target. Open a match with `!betopen` first.")
        return

    item_key = "soap_shoes"
    item = SHOP_ITEMS[item_key]
    price = compute_price(item_key)

    buyer_id = str(ctx.author.id)
    points = load_points()
    if buyer_id not in points:
        points[buyer_id] = STARTING_POINTS
        save_points(points)

    if points[buyer_id] < price:
        await ctx.send(f"You don't have enough points to buy Newt's Soap Shoes. You need {price} points.")
        return

    target_char = None
    if character.lower() == CURRENT_MATCH['char_a'].lower():
        target_char = CURRENT_MATCH['char_a']
        ratio_key = 'ratio_a'
        base_key = 'base_ratio_a'
    elif character.lower() == CURRENT_MATCH['char_b'].lower():
        target_char = CURRENT_MATCH['char_b']
        ratio_key = 'ratio_b'
        base_key = 'base_ratio_b'
    else:
        await ctx.send(f"Invalid character. Please pick `{CURRENT_MATCH['char_a']}` or `{CURRENT_MATCH['char_b']}`.")
        return

    # Deduct points from buyer
    points[buyer_id] -= price
    save_points(points)

    # Remove already-applied diapers for that character (if any) and reset ratio to base
    diapers = CURRENT_MATCH.get('diapers', {})
    removed_count = 0
    if target_char in diapers:
        removed_count = len(diapers[target_char])
        del diapers[target_char]
    CURRENT_MATCH['diapers'] = diapers

    # Reset ratio to base ratio (diapers removed). Protections prevent future diapering.
    old_ratio = CURRENT_MATCH[ratio_key]
    CURRENT_MATCH[ratio_key] = CURRENT_MATCH[base_key]
    new_ratio = CURRENT_MATCH[ratio_key]

    # Record protection application
    protections = CURRENT_MATCH.get('protections', {})
    if target_char not in protections:
        protections[target_char] = []
    protections[target_char].append({
        "name": item_key,
        "price": price,
        "buyer": buyer_id
    })
    CURRENT_MATCH['protections'] = protections

    # Send the required phrase and info
    await ctx.send(f"{ctx.author.mention} bought **Newt's Soap Shoes** for **{target_char}** for {price} points. uh meow?\nRemoved {removed_count} diapers. {target_char}'s odds changed {old_ratio:.2f}x -> {new_ratio:.2f}x.")

@bot.command()
async def buyring(ctx, member: discord.Member):
    """Buy a wedding ring and give the +0.5 payout bonus to the mentioned user.
    Usage: !buyring @user"""
    buyer_id = str(ctx.author.id)
    target_id = str(member.id)

    item = SHOP_ITEMS["wedding_ring"]
    price = compute_price("wedding_ring")
    bonus = item["bonus"]

    points = load_points()
    if buyer_id not in points:
        points[buyer_id] = STARTING_POINTS
        save_points(points)

    if points[buyer_id] < price:
        await ctx.send(f"You don't have enough points to buy a wedding ring. You need {price} points.")
        return

    # Deduct cost from buyer
    points[buyer_id] -= price
    save_points(points)

    # Load existing rings and add/merge bonus for target
    rings = load_rings()
    prev = float(rings.get(target_id, 0))
    rings[target_id] = prev + bonus  # allow stacking if desired
    save_rings(rings)
    await ctx.send(f"{ctx.author.mention} bought a wedding ring and gave <@{target_id}> a +{bonus:.1f} payout bonus. (Use this to 'erp' someone.)")


if __name__ == '__main__':
    # Read token from environment (supports .env via load_dotenv above)
    token = os.environ.get('DISCORD_TOKEN') or os.environ.get('TOKEN')
    if not token:
        print("Discord token not found. Set DISCORD_TOKEN or TOKEN environment variable (or add to .env).")
    else:
        try:
            bot.run(token)
        except Exception as e:
            print("Failed to start bot:", e)
