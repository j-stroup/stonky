"""Replay the rules over historical prices, one trading day at a time.

Each day:
  1. At the open: fill orders decided yesterday (sells first, then buys).
  2. At the close: update every position and record account value.
  3. After the close: decide tomorrow's sells and buys using today's data only.
"""

from dataclasses import dataclass, field

import pandas as pd

from stonks.config import BacktestConfig
from stonks.strategy import Position, add_indicators, dip_size, exit_reason, wants_to_buy


@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    days_held: int
    reason: str


@dataclass
class BacktestResult:
    equity: pd.Series                     # account value at each close
    benchmark: pd.Series                  # buy-and-hold benchmark value at each close
    trades: list[Trade]
    open_positions: list[Position]
    exposure: pd.Series                   # fraction of the account in trades at each close
    parked: pd.Series                     # fraction of the account parked in the benchmark
    config: BacktestConfig
    log: list[str] = field(default_factory=list)


def run_backtest(
    prices: dict[str, pd.DataFrame],
    benchmark_prices: pd.DataFrame,
    cfg: BacktestConfig,
    park_prices: pd.DataFrame | None = None,
) -> BacktestResult:
    """park_prices: daily bars for rules.park_in. Not needed when parking in the benchmark."""
    rules = cfg.rules
    park_name = rules.park_in
    if park_name and park_prices is None:
        if park_name != cfg.benchmark:
            raise ValueError(f"park_prices are required to park cash in {park_name}")
        park_prices = benchmark_prices
    start = pd.Timestamp(cfg.start)
    data = {t: add_indicators(df, rules) for t, df in prices.items()}

    calendar = sorted(set().union(*(df.index for df in data.values())))
    calendar = [d for d in calendar if d >= start]
    if not calendar:
        raise ValueError("No trading days in the requested range")

    cash = cfg.starting_cash
    positions: dict[str, Position] = {}
    last_close: dict[str, float] = {}
    pending_sells: dict[str, str] = {}   # ticker -> reason
    pending_buys: list[str] = []
    prev_equity = cfg.starting_cash

    parked_shares = 0
    last_park_close = 0.0

    trades: list[Trade] = []
    log: list[str] = []
    equity_curve, exposure_curve, parked_curve = {}, {}, {}

    def bar(ticker: str, date: pd.Timestamp):
        df = data[ticker]
        return df.loc[date] if date in df.index else None

    def park_bar_on(date: pd.Timestamp):
        if not park_name or date not in park_prices.index:
            return None
        return park_prices.loc[date]

    for date in calendar:
        day = date.date()

        # 1. Fill yesterday's orders at today's open.
        for ticker, reason in list(pending_sells.items()):
            b = bar(ticker, date)
            if b is None:
                continue  # no trading in this ticker today; try again tomorrow
            pos = positions.pop(ticker)
            price = b["Open"] * (1 - rules.slippage_pct)
            proceeds = pos.shares * price - rules.commission_per_trade
            cash += proceeds
            cost = pos.shares * pos.entry_price
            trades.append(Trade(
                ticker, pos.entry_date, pos.entry_price, date, price, pos.shares,
                pnl=proceeds - cost, return_pct=price / pos.entry_price - 1,
                days_held=pos.days_held, reason=reason,
            ))
            log.append(f"{day} SELL {pos.shares} {ticker} @ {price:.2f}: {reason}")
            del pending_sells[ticker]

        park_bar = park_bar_on(date)
        buys = [t for t in pending_buys if bar(t, date) is not None]
        buys = buys[:max(rules.max_positions - len(positions), 0)]

        # Sell parked shares if the day's buys need more cash than we have.
        if buys and park_bar is not None and parked_shares > 0:
            needed = len(buys) * (rules.max_position_pct * prev_equity + rules.commission_per_trade)
            shortfall = needed - cash
            if shortfall > 0:
                price = park_bar["Open"] * (1 - rules.slippage_pct)
                shares = min(parked_shares, int(shortfall // price) + 1)
                parked_shares -= shares
                cash += shares * price - rules.commission_per_trade
                log.append(f"{day} UNPARK {shares} {park_name} @ {price:.2f} to fund buys")

        for ticker in buys:
            b = bar(ticker, date)
            price = b["Open"] * (1 + rules.slippage_pct)
            budget = min(rules.max_position_pct * prev_equity, cash - rules.commission_per_trade)
            shares = int(budget // price)
            if shares < 1:
                continue
            cash -= shares * price + rules.commission_per_trade
            positions[ticker] = Position(ticker, shares, price, date)
            log.append(f"{day} BUY  {shares} {ticker} @ {price:.2f}")
        pending_buys = []

        # Park whatever cash is left over.
        if park_bar is not None and cash - rules.commission_per_trade >= rules.min_park_amount:
            price = park_bar["Open"] * (1 + rules.slippage_pct)
            shares = int((cash - rules.commission_per_trade) // price)
            if shares >= 1:
                parked_shares += shares
                cash -= shares * price + rules.commission_per_trade
                log.append(f"{day} PARK {shares} {park_name} @ {price:.2f}")

        # 2. At the close: update positions and record account value.
        for ticker, pos in positions.items():
            b = bar(ticker, date)
            if b is not None:
                last_close[ticker] = b["Close"]
                pos.update(b["Close"], rules)
        b = park_bar_on(date)
        if b is not None:
            last_park_close = b["Close"]
        invested = sum(p.shares * last_close[t] for t, p in positions.items())
        parked = parked_shares * last_park_close
        equity = cash + invested + parked
        equity_curve[date] = equity
        exposure_curve[date] = invested / equity if equity else 0.0
        parked_curve[date] = parked / equity if equity else 0.0

        # 3. Decide tomorrow's orders.
        for ticker, pos in positions.items():
            b = bar(ticker, date)
            if b is None or ticker in pending_sells:
                continue
            reason = exit_reason(pos, b["Close"], rules)
            if reason:
                pending_sells[ticker] = reason

        free_slots = rules.max_positions - len(positions) + len(pending_sells)
        candidates = []
        for ticker in data:
            if ticker in positions:
                continue
            b = bar(ticker, date)
            if b is not None and wants_to_buy(b["Close"], b["sma_fast"], b["sma_trend"], rules):
                candidates.append((dip_size(b["Close"], b["sma_fast"]), ticker))
        candidates.sort(reverse=True)  # biggest dips first
        pending_buys = [t for _, t in candidates[:max(free_slots, 0)]]

        prev_equity = equity

    equity = pd.Series(equity_curve, name="equity")
    benchmark = _buy_and_hold(benchmark_prices, equity.index, cfg)
    return BacktestResult(
        equity=equity,
        benchmark=benchmark,
        trades=trades,
        open_positions=list(positions.values()),
        exposure=pd.Series(exposure_curve, name="exposure"),
        parked=pd.Series(parked_curve, name="parked"),
        config=cfg,
        log=log,
    )


def _buy_and_hold(prices: pd.DataFrame, dates: pd.Index, cfg: BacktestConfig) -> pd.Series:
    """Spend all the starting cash on the benchmark at the first open and never sell."""
    df = prices.loc[dates[0]:dates[-1]]
    entry = df["Open"].iloc[0] * (1 + cfg.rules.slippage_pct)
    shares = int(cfg.starting_cash // entry)
    leftover = cfg.starting_cash - shares * entry
    value = shares * df["Close"] + leftover
    return value.reindex(dates).ffill().rename("benchmark")
