"""Every setting for the bot lives here, so you only have one file to tweak."""

# ---------------------------------------------------------------------------
# Alpaca endpoints. The trading URL is HARDCODED to the paper endpoint.
# There is deliberately no setting to switch to live trading.
# ---------------------------------------------------------------------------
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# Free Alpaca accounts can only read recent stock bars from the IEX feed.
# If you pay for the SIP feed you can change this to "sip".
LIVE_STOCK_FEED = "iex"
BACKTEST_STOCK_FEED = "sip"  # old data (older than 15 minutes) is free on SIP

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 10_000.0

# Every order this bot sends starts with this tag, so it never touches orders
# from your momentum bot (or anything else) on the same account.
BOT_TAG = "mr2"

# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------
# Order matters: when several symbols signal at once, earlier ones go first.
SYMBOLS = ["SPY", "QQQ", "GLD", "USO", "BTC/USD"]
MEAN_REVERSION_SYMBOLS = {"SPY", "QQQ", "BTC/USD"}
TREND_SYMBOLS = {"GLD", "USO"}
CRYPTO_SYMBOLS = {"BTC/USD"}
CLOSE_BEFORE_BELL = {"SPY", "QQQ"}  # never held overnight
# "Risk-on" assets tend to move together. Only one of them may be open at a time.
RISK_ON_SYMBOLS = {"SPY", "QQQ", "BTC/USD"}

# ---------------------------------------------------------------------------
# Strategy settings
# ---------------------------------------------------------------------------
# Mean reversion (SPY, QQQ, BTC) on 15-minute bars
MR_TIMEFRAME = "15Min"
MR_BAR_MINUTES = 15
MR_LOOKBACK = 20          # 20-bar moving average and standard deviation
MR_ENTRY_Z = -2.0         # buy when price is 2+ standard deviations below average
MR_STOP_ATR = 2.0         # stop loss = entry - 2 x ATR

# Trend following (GLD, USO) on daily bars
TREND_TIMEFRAME = "1Day"
TREND_FAST = 20           # 20 EMA
TREND_SLOW = 50           # 50 EMA
TREND_STOP_ATR = 3.0      # stop loss = entry - 3 x ATR (daily ATR)

ATR_PERIOD = 14

# ---------------------------------------------------------------------------
# Risk rules
# ---------------------------------------------------------------------------
RISK_PER_TRADE = 0.01             # lose at most 1% of the account if the stop is hit
BTC_RISK_PER_TRADE = 0.005        # BTC: smaller size, 0.5%
MAX_POSITION_PCT = 0.33           # no single position bigger than 33% of the account
BTC_MAX_POSITION_PCT = 0.15       # BTC: smaller cap
MAX_OPEN_POSITIONS = 3
DAILY_LOSS_LIMIT = 0.03           # stop opening trades for the day after a 3% drop

# Market-close handling for SPY/QQQ (minutes before the closing bell).
# GitHub's scheduler can run a few minutes late, so these are generous.
NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE = 50
FLATTEN_MINUTES_BEFORE_CLOSE = 35

# Ignore a 15-minute signal if its bar finished more than this long ago.
MAX_SIGNAL_AGE_MINUTES = 30

# Crypto stops must be stop-LIMIT orders on Alpaca. The limit sits this far
# below the stop so it still fills in a fast drop.
CRYPTO_STOP_LIMIT_BUFFER = 0.01

# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
# Alpaca stock trades are commission free (tiny SEC/FINRA sell fees ignored).
# Alpaca crypto taker fee is 0.25% at the lowest volume tier.
STOCK_FEE_RATE = 0.0
CRYPTO_FEE_RATE = 0.0025
# Extra price slippage assumed in the BACKTEST (live uses real fill prices).
STOCK_SLIPPAGE = 0.0003           # 0.03% per side
CRYPTO_SLIPPAGE = 0.0005          # 0.05% per side

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
DATA_DIR = "data"
STATE_FILE = f"{DATA_DIR}/state.json"
TRADES_FILE = f"{DATA_DIR}/trades.csv"
PERFORMANCE_FILE = f"{DATA_DIR}/performance.csv"
SUMMARY_FILE = f"{DATA_DIR}/summary.md"
CHART_FILE = f"{DATA_DIR}/bot_vs_spy.png"
LOG_FILE = f"{DATA_DIR}/bot.log"

BACKTEST_DIR = "backtest_results"
BACKTEST_REPORT = f"{BACKTEST_DIR}/backtest_report.md"
