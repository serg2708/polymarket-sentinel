import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "agent.db"
PROMPT_PATH = BASE / "prompt.md"
KILL_SWITCH = BASE / "KILL"          # touch KILL -> agent stops doing anything

MODE = os.getenv("AGENT_MODE", "paper")          # paper | live
BANKROLL_USD = float(os.getenv("AGENT_BANKROLL", "300"))

# --- sizing / risk (hard limits, the model cannot override these) ---
KELLY_FRACTION = 0.25            # quarter Kelly
MAX_POSITION_FRAC = 0.05         # max 5% of bankroll per position
MAX_TOTAL_EXPOSURE_FRAC = 0.40   # max 40% of bankroll in open positions
MAX_NEW_STAKE_PER_DAY_FRAC = 0.15
MAX_OPEN_POSITIONS = 10
MAX_DRAWDOWN = 0.30              # equity (incl. unrealized) -30% -> KILL file is created
MIN_EDGE = 0.07                  # p_model - all-in cost per share (ask + fee), to act
MIN_CONFIDENCE = 0.6
MIN_STAKE_USD = 2.0

# --- market selection ---
MARKETS_PER_RUN = 12             # one claude -p call per run
REEVAL_HOURS = 24                # don't re-forecast the same market more often
CANDIDATE_POOL = 2000            # top markets by 24h volume; short-horizon filter needs a wide pool
MAX_DESCRIPTION_CHARS = 6000     # full resolution rules; exclusions often sit at the end
MIN_LIQUIDITY = 5000
MIN_DAYS_TO_END = 2
MAX_DAYS_TO_END = 21             # short markets resolve fast -> quicker edge verdict
EXCLUDE_KEYWORDS = ["up or down", "tweets", "temperature", "o/u", "spread"]   # whole-word match

# Sources that reveal the market price. A forecast citing them is "leaked": stored, never traded,
# excluded from the Brier comparison.
PRICE_LEAK_DOMAINS = ["polymarket.com", "kalshi.com", "manifold.markets", "metaculus.com",
                      "predictit.org", "polymarketanalytics.com", "oddschecker.com", "betfair.com",
                      "electionbettingodds.com", "sportsbook", "draftkings.com", "fanduel.com"]

# Taker fee per share = rate * p * (1 - p) (docs.polymarket.com, fees). Rate is 0.04-0.07 by category,
# 0 for fee-free markets; /fee-rate only tells us whether fees apply, so assume the worst category.
TAKER_FEE_RATE = 0.07

# Bump when the prompt or the model's inputs change: the report scores only the current version,
# so forecasts made under old conditions don't pollute the edge verdict.
# v1: descriptions cut at 1200 chars.  v2: full resolution rules.
FORECAST_VERSION = 2

# --- claude code ---
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT_S = 900
CLAUDE_MAX_TURNS = 50            # ~3 searches x 12 markets
