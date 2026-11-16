"""Quick summary of the Alpaca paper account: value, gain/loss, positions, trades.

    python report.py              # summary with the last 10 fills
    python report.py --fills 30   # show more fills
    python report.py --out /var/www/stonks/report.txt   # write it to a file (for the web page)

Read-only: it never places or changes orders.
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from stonks.backtest import BacktestResult, run_backtest
from stonks.broker import ORDER_ID_PREFIX, AlpacaPaperBroker, load_env
from stonks.config import BacktestConfig, Rules
from stonks.data import load_recent_alpaca
from stonks.live import STATE_DIR, State

ROOT = Path(__file__).resolve().parent
PROBLEM_WORDS = ("ERROR", "FAILED", "HALTED")


@dataclass
class PositionRow:
    ticker: str
    qty: int
    avg_price: float
    price: float
    pl: float
    pl_pct: float


@dataclass
class FillRow:
    when: pd.Timestamp
    side: str
    ticker: str
    qty: int
    price: float


@dataclass
class Snapshot:
    equity: float
    cash: float
    start_value: float
    start_date: str
    positions: list[PositionRow]
    fills: list[FillRow]                      # oldest first, this bot's orders only
    state: State
    problems: list[str] = field(default_factory=list)
    stop_file: bool = False
    daily_equity: dict = field(default_factory=dict)   # date -> account value at that close


def realized_trades(fills: list[FillRow], skip: set[str]) -> list[float]:
    """Profit of each completed sell, using average cost. Tickers in skip are left out."""
    qty: dict[str, int] = {}
    cost: dict[str, float] = {}
    results = []
    for f in fills:
        if f.ticker in skip:
            continue
        if f.side == "buy":
            qty[f.ticker] = qty.get(f.ticker, 0) + f.qty
            cost[f.ticker] = cost.get(f.ticker, 0.0) + f.qty * f.price
        elif qty.get(f.ticker):
            avg = cost[f.ticker] / qty[f.ticker]
            sold = min(f.qty, qty[f.ticker])
            results.append(sold * (f.price - avg))
            qty[f.ticker] -= sold
            cost[f.ticker] -= sold * avg
    return results


def format_report(s: Snapshot, park: str | None, show_fills: int = 10, today: pd.Timestamp | None = None) -> str:
    today = today or pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    gain = s.equity - s.start_value
    gain_pct = gain / s.start_value if s.start_value else 0.0
    lines = [f"stonks paper account - {today:%a %b %d %Y}", ""]
    lines.append(f"Account value  ${s.equity:,.2f}   {gain:+,.2f} ({gain_pct:+.2%}) "
                 f"since start of ${s.start_value:,.2f}" + (f" on {s.start_date}" if s.start_date else ""))
    lines.append(f"Cash           ${s.cash:,.2f}")

    status = f"last ran {s.state.last_run}" if s.state.last_run else "no runs recorded on this machine"
    if s.state.halted:
        status += f"   ** HALTED: {s.state.halt_reason} **"
    if s.stop_file:
        status += "   ** STOP file present: not trading **"
    lines.append(f"Bot status     {status}")

    lines += ["", "Open positions"]
    if not s.positions:
        lines.append("  (none)")
    for p in sorted(s.positions, key=lambda p: (p.ticker == park, p.ticker)):
        label = "  idle cash" if p.ticker == park else ""
        held = ""
        if p.ticker in s.state.entries:
            days = len(pd.bdate_range(s.state.entries[p.ticker], today)) - 1
            held = f"   held {days} trading day{'s' if days != 1 else ''}"
        lines.append(f"  {p.ticker:5} {p.qty:>5} @ {p.avg_price:>8.2f} -> {p.price:>8.2f}   "
                     f"{p.pl:>+9,.2f} ({p.pl_pct:+.1%}){label}{held}")

    closed = realized_trades(s.fills, skip={park} if park else set())
    wins = sum(1 for r in closed if r > 0)
    lines += ["", f"Closed trades  {len(closed)} ({wins} won, {len(closed) - wins} lost), "
                  f"realized {sum(closed):+,.2f}" + (f" (not counting {park})" if park else "")]

    lines += ["", f"Recent fills (last {show_fills})"]
    recent = s.fills[-show_fills:]
    if not recent:
        lines.append("  (none yet)")
    for f in reversed(recent):
        lines.append(f"  {f.when:%Y-%m-%d}  {f.side.upper():4} {f.qty:>5} {f.ticker:5} @ {f.price:.2f}")

    if s.problems:
        lines += ["", "Recent problems (from journal.log)"]
        lines += [f"  {p}" for p in s.problems]
    return "\n".join(lines)


GAP_WARNING = 0.02   # flag a paper-vs-backtest gap bigger than this


@dataclass
class Comparison:
    start: pd.Timestamp          # first day the bot traded
    end: pd.Timestamp            # last complete day
    start_value: float
    paper_value: float
    paper_buys: set[str]
    backtest: BacktestResult


def format_comparison(c: Comparison) -> str:
    bt = c.backtest
    bt_value = bt.equity.iloc[-1]
    bt_buys = {t.ticker for t in bt.trades} | {p.ticker for p in bt.open_positions}
    paper_ret = c.paper_value / c.start_value - 1
    bt_ret = bt_value / c.start_value - 1
    gap = paper_ret - bt_ret
    lines = [f"Paper vs backtest of the same days ({c.start.date()} to {c.end.date()})",
             f"  Paper     ${c.paper_value:>10,.2f}  ({paper_ret:+.2%})   bought: {', '.join(sorted(c.paper_buys)) or '-'}",
             f"  Backtest  ${bt_value:>10,.2f}  ({bt_ret:+.2%})   bought: {', '.join(sorted(bt_buys)) or '-'}"]
    note = "normal: real fills vs. the open price, and paper pays no dividends"
    if abs(gap) > GAP_WARNING:
        note = "** bigger than expected: check the fills and journal.log for what differed **"
    lines.append(f"  Gap       {gap:+.2%}   ({note})")
    only_bt, only_paper = bt_buys - c.paper_buys, c.paper_buys - bt_buys
    if only_bt or only_paper:
        lines.append(f"  ** Different buys: only in backtest: {', '.join(sorted(only_bt)) or '-'}; "
                     f"only in paper: {', '.join(sorted(only_paper)) or '-'} **")
    return "\n".join(lines)


def compare_with_backtest(s: Snapshot, broker: AlpacaPaperBroker, today: pd.Timestamp) -> str:
    """Replay the rules over the days the bot has been trading and compare with the real paper account."""
    rules, base = Rules(), BacktestConfig()
    if not s.fills:
        return "Paper vs backtest: nothing to compare until the bot's first trades."
    first_run = s.fills[0].when.tz_convert("America/New_York").tz_localize(None).normalize()
    days = sorted(d for d in s.daily_equity if d < today)
    end = days[-1] if days else None
    if end is None or end < first_run:
        return "Paper vs backtest: needs at least one complete trading day after the first trades."

    tickers = list(base.universe) + [base.benchmark] + ([rules.park_in] if rules.park_in else [])
    bars = load_recent_alpaca(sorted(set(tickers)), today, broker.api_key, broker.secret_key,
                              calendar_days=(today - first_run).days + 450)
    # Start the replay on the close before the first run: that's the close the first decisions used.
    before = bars[base.universe[0]].index[bars[base.universe[0]].index < first_run]
    start = before[-1]
    start_value = s.daily_equity.get(start, s.start_value)
    cfg = BacktestConfig(start=str(start.date()), end=str(end.date()), starting_cash=start_value,
                         universe=list(base.universe), benchmark=base.benchmark, rules=rules)
    park = bars[rules.park_in] if rules.park_in else None
    result = run_backtest({t: bars[t] for t in base.universe}, bars[base.benchmark], cfg, park_prices=park)
    paper_buys = {f.ticker for f in s.fills if f.side == "buy" and f.ticker != rules.park_in}
    return format_comparison(Comparison(first_run, end, start_value, s.daily_equity[end], paper_buys, result))


def gather(broker: AlpacaPaperBroker, state_dir: Path) -> Snapshot:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

    c = broker.client
    a = c.get_account()
    positions = [PositionRow(p.symbol, int(float(p.qty)), float(p.avg_entry_price), float(p.current_price),
                             float(p.unrealized_pl), float(p.unrealized_plpc))
                 for p in c.get_all_positions()]

    history = c.get_portfolio_history(GetPortfolioHistoryRequest(period="all", timeframe="1D"))
    start_value = float(history.base_value or a.equity)
    start_date = ""
    daily_equity = {}
    if history.timestamp:
        start_date = f"{datetime.fromtimestamp(history.timestamp[0]):%Y-%m-%d}"
        for ts, eq in zip(history.timestamp, history.equity or []):
            if eq:   # skip days before the account existed
                day = pd.Timestamp(ts, unit="s", tz="UTC").tz_convert("America/New_York")
                daily_equity[day.tz_localize(None).normalize()] = float(eq)

    # Page through every closed order (Alpaca returns at most 500 at a time, newest first).
    orders, until = [], None
    while True:
        page = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, until=until))
        orders += page
        if len(page) < 500:
            break
        until = min(o.submitted_at for o in page)
    fills = [FillRow(pd.Timestamp(o.filled_at), str(getattr(o.side, "value", o.side)), o.symbol,
                     int(float(o.filled_qty)), float(o.filled_avg_price))
             for o in orders
             if (o.client_order_id or "").startswith(ORDER_ID_PREFIX) and o.filled_at and float(o.filled_qty or 0) > 0]
    fills.sort(key=lambda f: f.when)

    problems = []
    journal = state_dir / "journal.log"
    if journal.exists():
        lines = journal.read_text(encoding="utf-8").splitlines()
        problems = [l for l in lines if any(w in l for w in PROBLEM_WORDS)][-5:]

    return Snapshot(float(a.equity), float(a.cash), start_value, start_date, positions, fills,
                    State.load(state_dir / "state.json"), problems, (ROOT / "STOP").exists(), daily_equity)


def build_report(fills: int) -> tuple[str, bool]:
    """The full report text, and whether the account could be read."""
    load_env(ROOT / ".env")
    try:
        broker = AlpacaPaperBroker()
        snap = gather(broker, STATE_DIR / "alpaca")
    except Exception as e:
        return f"Couldn't read the Alpaca account: {e!r}", False
    parts = [format_report(snap, Rules().park_in, fills), ""]
    try:
        today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
        parts.append(compare_with_backtest(snap, broker, today))
    except Exception as e:
        parts.append(f"Paper vs backtest: couldn't run the comparison ({e!r})")
    return "\n".join(parts), True


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step, so a web page never shows half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(path)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fills", type=int, default=10, help="how many recent fills to show")
    p.add_argument("--out", type=Path, help="write the report to this file instead of printing it")
    args = p.parse_args()

    text, ok = build_report(args.fills)
    if args.out:
        stamp = pd.Timestamp.now(tz="America/New_York").strftime("%a %Y-%m-%d %I:%M %p ET")
        write_atomic(args.out, f"Generated {stamp}\n\n{text}\n")
    else:
        print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
