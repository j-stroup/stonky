"""Quick summary of the Alpaca paper account: value, gain/loss, positions, trades.

    python report.py              # summary with the last 10 fills
    python report.py --fills 30   # show more fills

Read-only: it never places or changes orders.
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from stonks.broker import ORDER_ID_PREFIX, AlpacaPaperBroker, load_env
from stonks.config import Rules
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
    if history.timestamp:
        start_date = f"{datetime.fromtimestamp(history.timestamp[0]):%Y-%m-%d}"

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
                    State.load(state_dir / "state.json"), problems, (ROOT / "STOP").exists())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fills", type=int, default=10, help="how many recent fills to show")
    args = p.parse_args()

    load_env(ROOT / ".env")
    try:
        snap = gather(AlpacaPaperBroker(), STATE_DIR / "alpaca")
    except Exception as e:
        print(f"Couldn't read the Alpaca account: {e!r}")
        return 1
    print(format_report(snap, Rules().park_in, args.fills))
    return 0


if __name__ == "__main__":
    sys.exit(main())
