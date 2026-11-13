"""The trading rules, kept separate from everything else.

The backtester, and later the paper and live traders, all call these same
functions, so what you test is exactly what runs.

Every decision here uses only data up to and including the day's close.
Orders are then filled at the next day's open.
"""

from dataclasses import dataclass

import pandas as pd

from stonks.config import Rules


def add_indicators(prices: pd.DataFrame, rules: Rules) -> pd.DataFrame:
    """Add the moving averages the entry rule needs. Expects a 'Close' column."""
    df = prices.copy()
    df["sma_fast"] = df["Close"].rolling(rules.fast_sma_days).mean()
    df["sma_trend"] = df["Close"].rolling(rules.trend_sma_days).mean()
    return df


def dip_size(close: float, sma_fast: float) -> float:
    """How far below the short-term average the price is (0.05 = 5% below)."""
    return 1.0 - close / sma_fast


def wants_to_buy(close: float, sma_fast: float, sma_trend: float, rules: Rules) -> bool:
    if pd.isna(sma_fast) or pd.isna(sma_trend):
        return False  # not enough history yet
    if rules.require_uptrend and close < sma_trend:
        return False
    return dip_size(close, sma_fast) >= rules.dip_pct


@dataclass
class Position:
    ticker: str
    shares: int
    entry_price: float
    entry_date: pd.Timestamp
    days_held: int = 0
    days_in_slow_loss: int = 0

    def gain(self, price: float) -> float:
        return price / self.entry_price - 1.0

    def update(self, close: float, rules: Rules) -> None:
        """Call once per trading day after the close."""
        self.days_held += 1
        if self.gain(close) <= -rules.slow_loss_pct:
            self.days_in_slow_loss += 1
        else:
            self.days_in_slow_loss = 0


def exit_reason(position: Position, close: float, rules: Rules) -> str | None:
    """Return why this position should be sold, or None to keep holding."""
    gain = position.gain(close)
    if gain >= rules.take_profit_pct:
        return f"take profit ({gain:+.1%})"
    if gain <= -rules.stop_loss_pct:
        return f"stop loss ({gain:+.1%})"
    if position.days_in_slow_loss >= rules.slow_loss_days:
        return f"slow loss: {position.days_in_slow_loss} days at or below -{rules.slow_loss_pct:.0%}"
    if position.days_held >= rules.max_hold_days:
        return f"max hold {rules.max_hold_days} days ({gain:+.1%})"
    return None
