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
MAX_DRAWDOWN = 0.30              # bankroll -30% -> KILL file is created
MIN_EDGE = 0.07                  # |p_model - price| to act
MIN_CONFIDENCE = 0.6
MIN_STAKE_USD = 2.0

# --- market selection ---
MARKETS_PER_RUN = 8              # one claude -p call per run, keep small for limits
REEVAL_HOURS = 24                # don't re-forecast the same market more often
MIN_LIQUIDITY = 5000
MIN_DAYS_TO_END = 2
MAX_DAYS_TO_END = 60
EXCLUDE_KEYWORDS = ["up or down", "tweets", "temperature", "o/u", "spread"]

# --- claude code ---
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT_S = 900
CLAUDE_MAX_TURNS = 30
