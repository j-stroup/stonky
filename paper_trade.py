"""Run the strategy once for today. Schedule it once per trading day after the open.

    python paper_trade.py sim              # pretend account saved on this computer, no keys needed
    python paper_trade.py alpaca           # Alpaca PAPER account (keys in .env)
    python paper_trade.py alpaca --dry-run # show what it would do, place no orders

Safety:
  - Create a file named STOP in this folder to stop all trading immediately.
  - If the account falls 12% from its best value, new buys stop until you run --reset-halt.
  - Only the ETFs in stonks/config.py and the idle-cash ticker are ever traded.
  - Alpaca mode only accepts PAPER keys.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from stonks.broker import AlpacaPaperBroker, SimBroker, load_env
from stonks.config import BacktestConfig, Safety
from stonks.data import load_recent
from stonks.live import STATE_DIR, Journal, State, run_day

ROOT = Path(__file__).resolve().parent


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["sim", "alpaca"])
    p.add_argument("--dry-run", action="store_true", help="show the plan but place no orders")
    p.add_argument("--force", action="store_true", help="run even if it already ran today")
    p.add_argument("--reset-halt", action="store_true", help="clear a safety halt after you've reviewed it")
    p.add_argument("--sim-cash", type=float, default=10_000.0, help="starting cash for a new sim account")
    args = p.parse_args()

    cfg, safety = BacktestConfig(), Safety()
    rules = cfg.rules
    state_dir = STATE_DIR / args.mode
    state_path = state_dir / "state.json"
    journal = Journal(None if args.dry_run else state_dir / "journal.log")
    today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()

    if (ROOT / "STOP").exists():
        journal("STOP file found: not trading. Delete it to resume.")
        return 0

    state = State.load(state_path)
    if state.last_run == str(today.date()) and not (args.force or args.dry_run):
        print(f"Already ran today ({today.date()}). Use --force to run again.")
        return 0

    tickers = list(cfg.universe) + ([rules.park_in] if rules.park_in else [])
    try:
        bars = {t: load_recent(t, today) for t in tickers}
    except Exception as e:
        journal(f"ERROR loading prices, not trading: {e}")
        return 1

    if args.mode == "sim":
        if today.weekday() >= 5:
            print("Market is closed on weekends.")
            return 0
        prices = {t: df["Close"].iloc[-1] for t, df in bars.items()}
        broker = SimBroker(state_dir / "sim_account.json", prices, args.sim_cash, rules.slippage_pct)
    else:
        load_env(ROOT / ".env")
        broker = AlpacaPaperBroker()
        if not broker.market_is_open() and not args.dry_run:
            print("The market isn't open right now. Run between 9:30 AM and 4:00 PM Eastern on a trading day.")
            return 0

    if args.reset_halt:
        equity = broker.account().equity
        journal(f"Halt reset by user (was: {state.halt_reason or 'not halted'}); best value reset to ${equity:,.2f}")
        state.halted, state.halt_reason, state.peak_equity = False, "", equity

    try:
        run_day(broker, bars, cfg.universe, rules, safety, state, today, journal,
                dry_run=args.dry_run, equity_log=state_dir / "equity_history.csv")
    except Exception as e:
        journal(f"ERROR during run: {e!r}. Check the account before running again.")
        state.save(state_path)
        return 1

    if not args.dry_run:
        state.save(state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
