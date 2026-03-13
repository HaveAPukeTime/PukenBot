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
            try:
                self.selected_a = self.select_a.values[0]
                await interaction.response.send_message(f"Selected A: {self.selected_a}", ephemeral=True)
            except Exception as e:
                print("select_a_callback error:", e)
                try:
                    await interaction.response.send_message("An error occurred processing your selection.", ephemeral=True)
                except Exception:
                    pass

        async def select_b_callback(interaction: discord.Interaction):
            try:
                self.selected_b = self.select_b.values[0]
                await interaction.response.send_message(f"Selected B: {self.selected_b}", ephemeral=True)
            except Exception as e:
                print("select_b_callback error:", e)
                try:
                    await interaction.response.send_message("An error occurred processing your selection.", ephemeral=True)
                except Exception:
                    pass

        self.select_a.callback = select_a_callback
        self.select_b.callback = select_b_callback

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
            await interaction.response.send_message(
                f"**Betting is now open!**\n**{self.selected_a}** has odds of **{ratio_a:.2f}x**\n**{self.selected_b}** has odds of **{ratio_b:.2f}x**\nUse `!bet [character] [amount]` to place your bet. Use `!shop` to view purchasable items.",
                ephemeral=False
            )
            # disable view (prevent re-use)
            self.stop()
            try:
                # best-effort remove view from the original message; ignore failures but log them
                await interaction.message.edit(view=None)
            except Exception as e:
                print("start_btn: failed to edit original message to remove view:", e)
        except Exception as e:
            print("start_btn error:", e)
            try:
                await interaction.response.send_message("An internal error occurred starting the match.", ephemeral=True)
            except Exception:
                pass

# Interactive betting GUI: modals + view for users to place bets via buttons/selects
class BetAmountModal(discord.ui.Modal):
    def __init__(self, character: str, requester_id: str):
        super().__init__(title=f"Bet on {character}")
        self.character = character
        self.requester_id = requester_id
        # single text input for amount
        self.amount = discord.ui.TextInput(label="Amount (integer)", style=discord.TextStyle.short, placeholder="Enter points to bet", required=True)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # parse and validate
            try:
                amt = int(self.amount.value.strip())
            except Exception:
                await interaction.response.send_message("Please enter a valid integer amount.", ephemeral=True)
                return

            if amt <= 0:
                await interaction.response.send_message("You must bet a positive amount.", ephemeral=True)
                return

            # global state
            global BETTING_OPEN, CURRENT_MATCH, BETS
            if not BETTING_OPEN or not CURRENT_MATCH:
                await interaction.response.send_message("No active betting round.", ephemeral=True)
                return

            user_id = str(interaction.user.id)
            points = load_points()
            if user_id not in points:
                points[user_id] = STARTING_POINTS

            if points[user_id] < amt:
                await interaction.response.send_message(f"You don't have enough points. You have {points[user_id]}.", ephemeral=True)
                return

            # check existing bet
            if user_id in BETS.get(CURRENT_MATCH['char_a'], {}) or user_id in BETS.get(CURRENT_MATCH['char_b'], {}):
                await interaction.response.send_message("You have already placed a bet for this match. Use the edit command to change it.", ephemeral=True)
                return

            # determine exact bet key name (case sensitive keys in BETS)
            if self.character.lower() == CURRENT_MATCH['char_a'].lower():
                bet_key = CURRENT_MATCH['char_a']
            elif self.character.lower() == CURRENT_MATCH['char_b'].lower():
                bet_key = CURRENT_MATCH['char_b']
            else:
                await interaction.response.send_message("Invalid character for the current match.", ephemeral=True)
                return

            # deduct points and store bet
            points[user_id] -= amt
            save_points(points)
            BETS.setdefault(bet_key, {})[user_id] = amt

            # update GUI (if open)
            await update_betting_gui()

            # ring announcement if present
            rings = load_rings()
            if user_id in rings and float(rings.get(user_id, 0)) > 0:
                # short public confirmation plus required phrase
                await interaction.response.send_message(f"{interaction.user.mention} placed {amt} on {bet_key}. adding littles up!", ephemeral=False)
            else:
                await interaction.response.send_message(f"{interaction.user.mention} placed {amt} on {bet_key}.", ephemeral=False)
        except Exception as e:
            print("BetAmountModal.on_submit error:", e)
            try:
                await interaction.response.send_message("An internal error occurred placing your bet.", ephemeral=True)
            except Exception:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print("BetAmountModal.on_error:", error)
        try:
            await interaction.response.send_message("An error occurred processing the bet.", ephemeral=True)
        except Exception:
            pass

# Build an embed for the current betting odds
def build_betting_embed() -> discord.Embed:
    """Build an up-to-date embed summarizing current match odds and bets."""
    if not CURRENT_MATCH:
        return discord.Embed(title="No active match", description="There is no betting match open.", color=discord.Color.dark_grey())

    char_a = CURRENT_MATCH['char_a']
    char_b = CURRENT_MATCH['char_b']
    ratio_a = float(CURRENT_MATCH.get('ratio_a', CURRENT_MATCH.get('base_ratio_a', 1.0)))
    ratio_b = float(CURRENT_MATCH.get('ratio_b', CURRENT_MATCH.get('base_ratio_b', 1.0)))

    bets_a = BETS.get(char_a, {}) if isinstance(BETS, dict) else {}
    bets_b = BETS.get(char_b, {}) if isinstance(BETS, dict) else {}
    total_a = sum(bets_a.values()) if bets_a else 0
    total_b = sum(bets_b.values()) if bets_b else 0
    total = total_a + total_b

    # implied probabilities from decimal odds (not accounting for vig)
    implied_a = (1.0 / ratio_a) if ratio_a > 0 else 0.0
    implied_b = (1.0 / ratio_b) if ratio_b > 0 else 0.0

    market_share_a = (total_a / total) if total > 0 else 0.0
    market_share_b = (total_b / total) if total > 0 else 0.0

    desc = (
        f"Betting is open for:\n"
        f"**A:** {char_a} — {ratio_a:.2f}x ({implied_a*100:.1f}% implied)\n"
        f"**B:** {char_b} — {ratio_b:.2f}x ({implied_b*100:.1f}% implied)\n\n"
        f"Total staked: {total} points\n"
    )

    embed = discord.Embed(title="Place Your Bets", description=desc, color=discord.Color.blurple())
    embed.add_field(
        name=f"{char_a}",
        value=(
            f"Odds: {ratio_a:.2f}x\n"
            f"Bets: {total_a} pts\n"
            f"Bettors: {len(bets_a)}\n"
            f"Market share: {market_share_a*100:.1f}%"
        ),
        inline=True
    )
    embed.add_field(
        name=f"{char_b}",
        value=(
            f"Odds: {ratio_b:.2f}x\n"
            f"Bets: {total_b} pts\n"
            f"Bettors: {len(bets_b)}\n"
            f"Market share: {market_share_b*100:.1f}%"
        ),
        inline=True
    )
    return embed

class BettingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _ensure_active_match(self, interaction: discord.Interaction):
        if not BETTING_OPEN or not CURRENT_MATCH:
            await interaction.response.send_message("There is no active betting match right now.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Bet on A", style=discord.ButtonStyle.primary, custom_id="betgui_bet_a")
    async def bet_a(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            if not await self._ensure_active_match(interaction):
                return
            await interaction.response.send_modal(BetAmountModal(CURRENT_MATCH['char_a'], str(interaction.user.id)))
        except Exception as e:
            print("bet_a error:", e)
            try:
                await interaction.response.send_message("Failed to open bet modal.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Bet on B", style=discord.ButtonStyle.danger, custom_id="betgui_bet_b")
    async def bet_b(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            if not await self._ensure_active_match(interaction):
                return
            await interaction.response.send_modal(BetAmountModal(CURRENT_MATCH['char_b'], str(interaction.user.id)))
        except Exception as e:
            print("bet_b error:", e)
            try:
                await interaction.response.send_message("Failed to open bet modal.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Bet All", style=discord.ButtonStyle.secondary, custom_id="betgui_bet_all")
    async def bet_all(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            if not await self._ensure_active_match(interaction):
                return
            user_id = str(interaction.user.id)
            points = load_points()
            if user_id not in points:
                points[user_id] = STARTING_POINTS
                save_points(points)
            amt = points[user_id]
            if amt <= 0:
                await interaction.response.send_message("You have no points to bet.", ephemeral=True)
                return

            if user_id in BETS.get(CURRENT_MATCH['char_a'], {}) or user_id in BETS.get(CURRENT_MATCH['char_b'], {}):
                await interaction.response.send_message("You have already placed a bet for this match.", ephemeral=True)
                return

            bet_key = CURRENT_MATCH['char_a']
            BETS.setdefault(bet_key, {})[user_id] = amt
            points[user_id] = 0
            save_points(points)

            # update GUI (if open)
            await update_betting_gui()

            rings = load_rings()
            if user_id in rings and float(rings.get(user_id, 0)) > 0:
                await interaction.response.send_message(f"{interaction.user.mention} bet all ({amt}) on {bet_key}. adding littles up!", ephemeral=False)
            else:
                await interaction.response.send_message(f"{interaction.user.mention} bet all ({amt}) on {bet_key}.", ephemeral=False)
        except Exception as e:
            print("bet_all error:", e)
            try:
                await interaction.response.send_message("Failed to place Bet All.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Bet Random", style=discord.ButtonStyle.success, custom_id="betgui_bet_random")
    async def bet_random(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            if not await self._ensure_active_match(interaction):
                return
            user_id = str(interaction.user.id)
            points = load_points()
            if user_id not in points:
                points[user_id] = STARTING_POINTS
                save_points(points)
            amt = 10
            if points[user_id] < amt:
                await interaction.response.send_message(f"You don't have enough points ({points[user_id]}) to random bet {amt}.", ephemeral=True)
                return
            if user_id in BETS.get(CURRENT_MATCH['char_a'], {}) or user_id in BETS.get(CURRENT_MATCH['char_b'], {}):
                await interaction.response.send_message("You have already placed a bet for this match.", ephemeral=True)
                return
            chosen = random.choice([CURRENT_MATCH['char_a'], CURRENT_MATCH['char_b']])
            points[user_id] -= amt
            save_points(points)
            BETS.setdefault(chosen, {})[user_id] = amt

            # update GUI (if open)
            await update_betting_gui()

            rings = load_rings()
            if user_id in rings and float(rings.get(user_id, 0)) > 0:
                await interaction.response.send_message(f"{interaction.user.mention} placed a random bet of {amt} on {chosen}. adding littles up!", ephemeral=False)
            else:
                await interaction.response.send_message(f"{interaction.user.mention} placed a random bet of {amt} on {chosen}.", ephemeral=False)
        except Exception as e:
            print("bet_random error:", e)
            try:
                await interaction.response.send_message("Failed to place random bet.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Refresh Odds", style=discord.ButtonStyle.gray, custom_id="betgui_refresh")
    async def refresh(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            if not await self._ensure_active_match(interaction):
                return
            embed = build_betting_embed()
            # edit the message that contains the view with updated embed
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            print("refresh error:", e)
            try:
                await interaction.response.send_message("Failed to refresh odds.", ephemeral=True)
            except Exception:
                pass

# Command to open the betting GUI for a current match
@bot.command()
async def openbetgui(ctx):
    """Open an interactive betting GUI (buttons + modals) for the current match."""
    if not BETTING_OPEN or not CURRENT_MATCH:
        await ctx.send("There is no active betting match. Start one with !betopen or use the GUI to create one.")
        return

    embed = build_betting_embed()
    view = BettingView()
    msg = await ctx.send(embed=embed, view=view)

    # store GUI message references so we can auto-update it later
    try:
        CURRENT_MATCH['gui_channel_id'] = ctx.channel.id
        CURRENT_MATCH['gui_message_id'] = msg.id
    except Exception as e:
        print("openbetgui: failed to store gui message info:", e)

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
