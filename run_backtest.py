"""Run a backtest from the command line.

    python run_backtest.py
    python run_backtest.py --start 2018-01-01 --take-profit 0.08 --stop-loss 0.04
    python run_backtest.py --tickers XLK XLV XLP --cash 5000
"""

import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

from stonks.backtest import run_backtest
from stonks.config import BacktestConfig, Rules
from stonks.data import load_prices
from stonks.report import mix, print_summary, save_results


def main() -> None:
    default = BacktestConfig()
    p = argparse.ArgumentParser(description="Backtest the trading rules in stonks/config.py")
    p.add_argument("--start", default=default.start)
    p.add_argument("--end", default=default.end)
    p.add_argument("--cash", type=float, default=default.starting_cash)
    p.add_argument("--tickers", nargs="+", default=default.universe)
    p.add_argument("--benchmark", default=default.benchmark)
    p.add_argument("--take-profit", type=float, help="e.g. 0.06 for +6%%")
    p.add_argument("--stop-loss", type=float, help="e.g. 0.05 for -5%%")
    p.add_argument("--position-size", type=float, help="max fraction of the account per position, e.g. 0.15")
    p.add_argument("--dip", type=float, help="buy when this far below the 20-day average, e.g. 0.03")
    p.add_argument("--park", help="ticker to hold idle cash in (default from config, e.g. BIL, SHY, SPY)")
    p.add_argument("--no-park", action="store_true", help="leave idle cash as plain cash")
    p.add_argument("--refresh", action="store_true", help="re-download prices instead of using the cache")
    p.add_argument("--out", default="results", help="folder for trades.csv, equity.csv, log.txt")
    args = p.parse_args()

    overrides = {}
    if args.take_profit is not None:
        overrides["take_profit_pct"] = args.take_profit
    if args.stop_loss is not None:
        overrides["stop_loss_pct"] = args.stop_loss
    if args.position_size is not None:
        overrides["max_position_pct"] = args.position_size
    if args.dip is not None:
        overrides["dip_pct"] = args.dip
    if args.park:
        overrides["park_in"] = args.park.upper()
    if args.no_park:
        overrides["park_in"] = None
    rules = replace(Rules(), **overrides)

    cfg = BacktestConfig(
        start=args.start, end=args.end, starting_cash=args.cash,
        universe=[t.upper() for t in args.tickers], benchmark=args.benchmark.upper(), rules=rules,
    )

    # Load extra history before the start date so the 200-day average is ready on day one.
    warmup_start = (pd.Timestamp(cfg.start) - pd.Timedelta(days=int(rules.trend_sma_days * 1.6))).strftime("%Y-%m-%d")
    prices = {t: load_prices(t, warmup_start, cfg.end, args.refresh) for t in cfg.universe}
    benchmark = load_prices(cfg.benchmark, warmup_start, cfg.end, args.refresh)

    park = load_prices(rules.park_in, warmup_start, cfg.end, args.refresh) if rules.park_in else None

    result = run_backtest(prices, benchmark, cfg, park_prices=park)

    # Compare with a no-trading stock/bond mix of similar risk.
    shy = park if rules.park_in == "SHY" else load_prices("SHY", warmup_start, cfg.end, args.refresh)
    w = 0.20
    yardstick = mix(benchmark, shy, w, cfg.start, cfg.end)
    print_summary(result, yardstick, f"{w:.0%} {cfg.benchmark} / {1 - w:.0%} SHY")
    save_results(result, Path(args.out))


if __name__ == "__main__":
    main()
