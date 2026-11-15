"""All tunable numbers live here, so the rules are decided before the market opens."""

from dataclasses import dataclass, field

# Sector ETFs: broad, liquid, and they existed for the whole test period
# (individual stocks picked today would bias a backtest toward winners).
#
# ET (Energy Transfer, a single partnership) was added on purpose as a more
# volatile position, to see how the rules behave on something that moves more.
DEFAULT_UNIVERSE = [
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB",
    "ET",
]


@dataclass(frozen=True)
class Rules:
    # --- When to sell ---------------------------------------------------
    take_profit_pct: float = 0.06    # sell once up 6%
    stop_loss_pct: float = 0.05      # sell immediately once down 5%
    slow_loss_pct: float = 0.02      # "slow bleed": down at least 2%...
    slow_loss_days: int = 10         # ...for this many trading days in a row
    max_hold_days: int = 60          # give up on a position after this long

    # --- When to buy ----------------------------------------------------
    fast_sma_days: int = 20          # short-term average
    dip_pct: float = 0.03            # buy when price is 3% below the short-term average
    trend_sma_days: int = 200        # long-term average
    require_uptrend: bool = True     # only buy dips when price is above the long-term average

    # --- How much to buy ------------------------------------------------
    max_position_pct: float = 0.10   # never put more than 10% of the account in one position
    max_positions: int = 8

    # --- Idle cash ------------------------------------------------------
    # Keep money that isn't in a trade invested in this ticker instead of
    # sitting in cash. Parked shares are sold automatically to fund new buys.
    #   "BIL" = 1-3 month Treasury bills: earns interest, barely moves
    #   "SHY" = 1-3 year Treasuries: a little more interest, small swings
    #   "SPY" = the stock market: much higher returns, but SPY-sized crashes
    #   None  = plain cash, earns nothing
    # Default picked by compare.py: best 2015-2020 return with a worst drop within -10%.
    park_in: str | None = "SHY"
    min_park_amount: float = 100.0   # don't bother parking less than this

    # --- Trading costs --------------------------------------------------
    commission_per_trade: float = 0.0
    slippage_pct: float = 0.0005     # assume each fill is 0.05% worse than the open price


@dataclass(frozen=True)
class Safety:
    """Limits for the paper (and later live) trader. These override the strategy."""
    # Stop all new buying if the account falls this far below its best value.
    # The backtest's worst drop was about -10%, so a bigger drop means something
    # is different from what was tested. It stays stopped until you reset it.
    halt_drawdown_pct: float = 0.12
    # No single trade (not counting idle-cash parking) may be bigger than this.
    max_trade_pct: float = 0.30
    # Size buys as if the price will open this much higher than the last close.
    price_buffer_pct: float = 0.01
    # Refuse to trade on price data older than this many calendar days.
    max_data_age_days: int = 5


@dataclass(frozen=True)
class BacktestConfig:
    start: str = "2015-01-01"
    end: str = "2025-12-31"
    starting_cash: float = 10_000.0
    universe: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    benchmark: str = "SPY"
    rules: Rules = field(default_factory=Rules)
