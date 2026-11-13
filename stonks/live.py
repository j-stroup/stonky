"""The once-a-day trading routine for paper (and later live) trading.

Run it once per trading day, shortly after the market opens. It:
  1. Looks at prices up to YESTERDAY's close (the last complete day).
  2. Decides sells and buys with the same rules the backtest uses.
  3. Sells first, sells parked shares if buys need cash, buys, then parks leftover cash.

That matches the backtest: decide on a close, fill at the next open.
"""

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from stonks.broker import Broker, Holding
from stonks.config import Rules, Safety
from stonks.strategy import Position, add_indicators, dip_size, exit_reason, wants_to_buy

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


# --- Saved state between runs --------------------------------------------

@dataclass
class State:
    entries: dict[str, str] = field(default_factory=dict)   # ticker -> date it was bought
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    last_run: str = ""

    @classmethod
    def load(cls, path: Path) -> "State":
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


class Journal:
    """Prints every line and appends it to a log file, so there's a record of every decision."""

    def __init__(self, path: Path | None):
        self.path = path

    def __call__(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
        print(line)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


# --- Deciding ------------------------------------------------------------

@dataclass
class Plan:
    sells: list[tuple[str, int, str]] = field(default_factory=list)   # (ticker, qty, reason)
    buys: list[str] = field(default_factory=list)                    # tickers, best first


def plan_day(bars: dict[str, pd.DataFrame], holdings: dict[str, Holding],
             entries: dict[str, str], universe: list[str], rules: Rules) -> Plan:
    """Decide today's orders from bars that end at the last complete day.

    bars must already have indicators (see add_indicators).
    """
    plan = Plan()
    managed = {t: h for t, h in holdings.items() if t in universe}

    for ticker, h in managed.items():
        df = bars[ticker]
        entry = pd.Timestamp(entries.get(ticker, df.index[-1]))
        closes = df["Close"][df.index >= entry]
        # Rebuild the position's day counters by replaying each close since it was bought.
        pos = Position(ticker, h.qty, h.avg_price, entry)
        for close in closes:
            pos.update(close, rules)
        reason = exit_reason(pos, df["Close"].iloc[-1], rules)
        if reason:
            plan.sells.append((ticker, h.qty, reason))

    free_slots = rules.max_positions - len(managed) + len(plan.sells)
    candidates = []
    for ticker in universe:
        if ticker in managed:
            continue
        last = bars[ticker].iloc[-1]
        if wants_to_buy(last["Close"], last["sma_fast"], last["sma_trend"], rules):
            candidates.append((dip_size(last["Close"], last["sma_fast"]), ticker))
    candidates.sort(reverse=True)
    plan.buys = [t for _, t in candidates[:max(free_slots, 0)]]
    return plan


def check_bars(bars: dict[str, pd.DataFrame], today: pd.Timestamp, safety: Safety) -> pd.Timestamp:
    """Refuse to trade on stale or mismatched data. Returns the last complete day."""
    last_dates = {t: df.index[-1] for t, df in bars.items()}
    if len(set(last_dates.values())) != 1:
        raise RuntimeError(f"Price data ends on different days: {last_dates}")
    last = next(iter(last_dates.values()))
    if last >= today.normalize():
        raise RuntimeError("Price data includes today's unfinished bar")
    if (today.normalize() - last).days > safety.max_data_age_days:
        raise RuntimeError(f"Price data is stale (last day {last.date()})")
    return last


# --- Doing ---------------------------------------------------------------

def run_day(broker: Broker, raw_bars: dict[str, pd.DataFrame], universe: list[str],
            rules: Rules, safety: Safety, state: State, today: pd.Timestamp,
            journal: Journal, dry_run: bool = False, equity_log: Path | None = None) -> Plan:
    bars = {t: add_indicators(df, rules) for t, df in raw_bars.items()}
    last_day = check_bars(bars, today, safety)
    last_close = {t: df["Close"].iloc[-1] for t, df in bars.items()}
    park = rules.park_in

    acct = broker.account()
    holdings = broker.holdings()
    journal(f"--- {today.date()} [{broker.name}{' DRY RUN' if dry_run else ''}] "
            f"decisions use closes from {last_day.date()} ---")
    journal(f"Account: equity ${acct.equity:,.2f}, cash ${acct.cash:,.2f}, holding "
            + (", ".join(f"{h.qty} {t}" for t, h in sorted(holdings.items())) or "nothing"))

    # Forget positions that are gone; note ones we didn't buy (e.g. bought by hand).
    for t in list(state.entries):
        if t not in holdings:
            del state.entries[t]
    for t in holdings:
        if t in universe and t not in state.entries:
            state.entries[t] = str(last_day.date())
            journal(f"NOTE: {t} is held but wasn't bought by this bot; counting its days from {last_day.date()}")

    # Account-level safety stop.
    state.peak_equity = max(state.peak_equity, acct.equity)
    drop = acct.equity / state.peak_equity - 1 if state.peak_equity else 0.0
    if not state.halted and drop <= -safety.halt_drawdown_pct:
        state.halted = True
        state.halt_reason = (f"{today.date()}: account fell {drop:.1%} from its best "
                             f"(${state.peak_equity:,.2f}), past the {safety.halt_drawdown_pct:.0%} limit")
    if state.halted:
        journal(f"HALTED: {state.halt_reason}. Sells still run; no new buys or parking. "
                f"Review, then run with --reset-halt.")

    plan = plan_day(bars, holdings, state.entries, universe, rules)
    if state.halted:
        plan.buys = []
    for t, q, reason in plan.sells:
        journal(f"PLAN sell {q} {t}: {reason}")
    for t in plan.buys:
        journal(f"PLAN buy {t}: {dip_size(last_close[t], bars[t]['sma_fast'].iloc[-1]):.1%} below its "
                f"{rules.fast_sma_days}-day average")
    if not plan.sells and not plan.buys:
        journal("PLAN nothing to trade today")
    if dry_run:
        return plan

    # 1. Sells.
    if plan.sells:
        for f in broker.market_orders([(t, "sell", q) for t, q, _ in plan.sells]):
            _log_fill(journal, f)
            if f.status == "filled":
                state.entries.pop(f.ticker, None)

    # 2. Buys, selling parked shares first if more cash is needed.
    acct = broker.account()
    budget = min(rules.max_position_pct, safety.max_trade_pct) * acct.equity
    est = {t: last_close[t] * (1 + safety.price_buffer_pct) for t in plan.buys}
    orders = [(t, int(budget // est[t])) for t in plan.buys]
    orders = [(t, q) for t, q in orders if q >= 1]
    needed = sum(q * est[t] for t, q in orders)
    parked = broker.holdings().get(park) if park else None
    if orders and parked and needed > acct.cash:
        park_price = last_close[park] * (1 - safety.price_buffer_pct)
        qty = min(parked.qty, math.ceil((needed - acct.cash) / park_price))
        journal(f"UNPARK {qty} {park} to pay for buys")
        for f in broker.market_orders([(park, "sell", qty)]):
            _log_fill(journal, f)
        acct = broker.account()

    if orders:
        cash_left = acct.cash
        sized = []
        for t, q in orders:
            q = min(q, int(cash_left // est[t]))
            if q >= 1:
                sized.append((t, "buy", q))
                cash_left -= q * est[t]
        for f in broker.market_orders(sized):
            _log_fill(journal, f)
            if f.qty > 0:
                state.entries[f.ticker] = str(today.date())

    # 3. Park leftover cash.
    acct = broker.account()
    if park and not state.halted and acct.cash >= rules.min_park_amount:
        qty = int(acct.cash // (last_close[park] * (1 + safety.price_buffer_pct)))
        if qty >= 1:
            journal(f"PARK {qty} {park}")
            for f in broker.market_orders([(park, "buy", qty)]):
                _log_fill(journal, f)

    acct = broker.account()
    state.peak_equity = max(state.peak_equity, acct.equity)
    state.last_run = str(today.date())
    journal(f"Done: equity ${acct.equity:,.2f}, cash ${acct.cash:,.2f}")
    if equity_log:
        _append_equity(equity_log, today, acct.equity, acct.cash)
    return plan


def _log_fill(journal: Journal, f) -> None:
    if f.qty > 0:
        journal(f"{f.side.upper()} {f.qty} {f.ticker} @ {f.price:.2f} ({f.status})")
    else:
        journal(f"FAILED {f.side} {f.ticker}: {f.status}")


def _append_equity(path: Path, today: pd.Timestamp, equity: float, cash: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new:
            f.write("date,equity,cash\n")
        f.write(f"{today.date()},{equity:.2f},{cash:.2f}\n")
