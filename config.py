"""All settings in one place. Secrets come from the .env file."""
import os
from datetime import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def _bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------- Credentials (.env) ----------
FYERS_APP_ID = os.getenv("FYERS_APP_ID", "")          # e.g. ABCD1234-100
FYERS_SECRET = os.getenv("FYERS_SECRET", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------- Mode ----------
PAPER_MODE = _bool("PAPER_MODE", True)                # True = simulated fills, no real orders
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "250000"))

# ---------- Universe ----------
# SIGNAL_SOURCE = FUTURES: signals, RSI, EMA, stops, targets and the order-book check all use the FUTURE's own
#                 price (like the first version). SPOT: scan the stock's cash price and trade only its future.
SIGNAL_SOURCE = os.getenv("SIGNAL_SOURCE", "FUTURES").strip().upper()
PRICE_LABEL = "Future" if SIGNAL_SOURCE == "FUTURES" else "Spot"
# ALL = every F&O underlying, or a comma list of underlyings, e.g. "RELIANCE,SBIN,NIFTY"
UNIVERSE = os.getenv("UNIVERSE", "ALL").strip().upper()
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
INCLUDE_INDICES = _bool("INCLUDE_INDICES", True)   # trade index futures too (needs SIGNAL_SOURCE=FUTURES)
CONTRACT_SWITCH_DAY = 22   # before the 22nd trade the current month's future, from the 22nd the next month's

# ---------- Strategy ----------
ARM_TF = 120            # minutes: RSI arming candle (was 4H on Delta)
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70     # closed 2H RSI above this arms SHORT (85 from 13-19 Nov 2026, back to 70)
SHORT_MIN_STOP_PCT = 0.005  # 2H RSI SHORT: stop at least 0.5% from entry
LONG_MIN_STOP_PCT = 0.005   # 2H RSI LONG: stop at least 0.5% from entry
# 2H RSI entry rule: "divergence" (since 19 Nov 2026) or "classic" (A green above EMA5 + B red / C1-C2).
# Divergence SHORT (5m): first peak = RED candle closing above EMA5 with its low above EMA5; within the next
#   RSI_DIV_SHORT_WINDOW_MIN a RED candle closes with a higher high and an RSI(14, 5m) at least RSI_DIV_MIN_RSI_DIFF
#   lower -> short at the next candle's open (if it opens below that red candle's body high); stop above its high.
# Divergence LONG (15m): first trough = GREEN candle with its high below EMA5; within RSI_DIV_LONG_WINDOW_MIN a GREEN
#   candle closes with a lower low and an RSI(14, 15m) at least RSI_DIV_MIN_RSI_DIFF higher -> long at the next open
#   (if above its body low); stop below its low.
RSI_ENTRY = "divergence"
RSI_DIV_SHORT_WINDOW_MIN = 45     # (30 until 20 Nov)
RSI_DIV_LONG_WINDOW_MIN = 120     # (75 until 20 Nov)
RSI_DIV_MIN_GAP = 2               # at least this many candles between the first and the second peak / dip
# The reference is the highest peak / lowest dip in the window. Since 23 Nov the first peak must be red (short) and
# the first dip green (long) (20-23 Nov: any colour).
RSI_DIV_MIN_RSI_DIFF = 3.0        # divergence counts only if the RSI differs by at least this many points (23 Nov)
RSI_ALWAYS_TRAIL = True     # 2H RSI trades: no fixed target, breakeven at 1R, trail from TRAIL_START_R (1H unchanged)
SHORT_COOLDOWN_MIN = 30 # 2H RSI SHORT: no new short on a stock until 30 min after its last short closed
RSI_OVERSOLD = 30       # closed 2H RSI below this arms LONG
SHORT_TF = 5            # short trigger timeframe
EMA_PERIOD = 5          # green candle's low must be above this EMA
LONG_TF = 15            # long trigger timeframe
BREAKOUT_TF = 60          # 1H consolidation candles
BREAKOUT_CANDLES = 3      # last 3 closed 1H candles of TODAY only...
BREAKOUT_RANGE_PCT = 0.01 # ...whose high-low range is within 1% of the low
BREAKOUT_CHECK_MINUTE = 12  # check the forming 15m candle at its 12th minute
# Opening check (23 Nov), once a day at 09:19:30, 4.5 min into the first 5m candle (09:15-09:20), before ENTRY_START:
# same 1H range (at the open: yesterday's last 3 closed 1H candles). Breakout: LTP > body high and the first 5m candle
# green so far (LTP > its open) -> LONG, stop 1 tick below that 5m candle's low (as of the check). Breakdown: LTP < body
# low and the 5m candle red so far -> SHORT, stop 1 tick above its high. The stop is fixed at entry, widened to at
# least OPEN_BO_MIN_STOP_PCT of the price (logged), still capped at MAX_LOSS_PCT. No 2R target: breakeven at 1R, trail
# from TRAIL_START_R. Same books and limits as the minute-12 check. Logged as "OPEN 5m".
BREAKOUT_OPEN_CHECK = time(9, 19, 30)
OPEN_BO_MIN_STOP_PCT = 0.003
IMBALANCE_MIN_SHARE = 0.55  # order book: longs need bids >= 55% of total book qty, shorts need asks >= 55%
# The filter was never backtested (no historical order books) and blocked nearly every signal, so it is OFF:
# the bid/ask share is still looked up and written into each signal/entry log for later analysis.
IMBALANCE_FILTER = False
RISK_REWARD = 2.0       # take-profit = 2 x stop distance (intraday only, so 1:2)
MAX_LOSS_PCT = 0.10     # stop never further than a loss of 10% of the whole account balance (measured on spot)
BACKUP_LOSS_PCT = 0.12  # live only: backup SL-M on the future at a 12% loss, in case the bot can't act
BREAKEVEN_AT_R = 1.0    # once price reaches 1:1, move the stop to entry
# Trend-day trail (all 3 strategies): if at entry today's open is within OPEN_EXTREME_TOL of the day's high (shorts)
# or the day's low (longs), the trade has NO fixed target. Breakeven at 1R still applies; from TRAIL_START_R the stop
# trails in whole-R steps TRAIL_GAP_R behind (3R -> stop at 2R, 4R -> 3R ...); exit on that stop or the 15:15 square-off.
OPEN_EXTREME_TOL = 0.001
TRAIL_START_R = 3
TRAIL_GAP_R = 1
MAX_LOTS = int(os.getenv("MAX_LOTS", "0"))   # 0 = no cap: buy as many lots as the balance's margin allows
DAILY_LOSS_LIMIT_PCT = 0.50   # stop new entries after losing 50% of the day's opening balance

# ---------- Session (IST) ----------
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
ENTRY_CUTOFF = time(15, 0)     # no new entries after this
ENTRY_START = time(9, 30)      # skip the opening 15 minutes: no entries before this, and setup candles
                               # (short A, long C1) must start at or after it
SQUARE_OFF = time(15, 15)      # close everything, profit or loss
LOGIN_PROMPT_TIME = time(8, 45)

# ---------- Paper-mode costs (rough estimate per round trip, INR) ----------
COST_PER_ROUND_TRIP = float(os.getenv("COST_PER_ROUND_TRIP", "150"))

# ---------- Plumbing ----------
API_CALLS_PER_SEC = 2.5
POSITION_POLL_SECONDS = 3
ORDER_TAG = "JYBOT"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
