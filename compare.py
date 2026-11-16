"""Find the settings that earn the most while keeping losses small.

    python compare.py                 # position size, dip size, idle cash, take-profit
    python compare.py --grid exits    # fixed take-profit vs. trailing stops

How it avoids fooling itself:
  1. Every combination in the grid is run on the TUNE years only.
  2. Combinations whose worst drop in the TUNE years was worse than MAX_DROP are thrown out.
  3. The rest are ranked by return per year in the TUNE years, and the top one is picked.
  4. Only then are the CHECK and OLD years shown. Those years played no part in the
     pick, so they're closer to what you'd see going forward. OLD includes 2008.
"""

import argparse
from dataclasses import replace
from itertools import product

import pandas as pd

from stonks.backtest import run_backtest
from stonks.config import BacktestConfig, Rules
from stonks.data import load_prices
from stonks.report import cagr, max_drawdown, mix, worst_year

TUNE = ("2015-01-01", "2020-12-31")
CHECK = ("2021-01-01", "2025-12-31")
OLD = ("2004-01-01", "2014-12-31")
PERIODS = {"tune": TUNE, "check": CHECK, "old": OLD}
MAX_DROP = -0.10   # the most you're willing to see the account fall from a high
SHOW = 12
MIX_WEIGHTS = [0.20, 0.30]   # stock/bond mixes to compare against: no trading at all

GRIDS = {
    "settings": {
        "park_in": [None, "BIL", "SHY"],
        "max_position_pct": [0.10, 0.15, 0.20, 0.25],
        "dip_pct": [0.03, 0.04],
        "take_profit_pct": [0.06, 0.10],
    },
    "exits": {
        "max_position_pct": [0.10, 0.15],
        "trail_after_pct": [None, 0.04, 0.06, 0.08],   # None = fixed take-profit (current)
        "trail_pct": [0.02, 0.03, 0.05],
    },
}


def describe(r: Rules) -> dict:
    if r.trail_after_pct is None:
        exit_rule = f"fixed +{r.take_profit_pct:.0%}"
    else:
        exit_rule = f"trail {r.trail_pct:.0%} after +{r.trail_after_pct:.0%}"
    return {"park": r.park_in or "cash", "size": f"{r.max_position_pct:.0%}",
            "dip": f"{r.dip_pct:.0%}", "exit": exit_rule}


def stats(series: pd.Series) -> tuple[float, float, float]:
    return cagr(series), max_drawdown(series), worst_year(series)[1]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--grid", choices=list(GRIDS), default="settings")
    grid = GRIDS[p.parse_args().grid]

    base = BacktestConfig()
    warmup = "2003-01-01"
    parks = {r for r in grid.get("park_in", [Rules().park_in]) if r}
    tickers = set(base.universe) | {base.benchmark, "SHY"} | parks
    data = {t: load_prices(t, warmup, CHECK[1]) for t in sorted(tickers)}

    def run(rules: Rules, period):
        cfg = BacktestConfig(start=period[0], end=period[1], rules=rules)
        prices = {t: data[t].loc[:period[1]] for t in base.universe}
        park = data[rules.park_in].loc[:period[1]] if rules.park_in else None
        return run_backtest(prices, data[base.benchmark].loc[:period[1]], cfg, park_prices=park)

    rows, seen, bench = [], set(), {}
    for values in product(*grid.values()):
        rules = replace(Rules(), **dict(zip(grid, values)))
        if rules.trail_after_pct is None:
            rules = replace(rules, trail_pct=Rules().trail_pct)   # trail_pct is unused: skip duplicates
        if rules in seen:
            continue
        seen.add(rules)
        row = describe(rules)
        for label, period in PERIODS.items():
            r = run(rules, period)
            row[f"{label} /yr"] = cagr(r.equity)
            row[f"{label} drop"] = max_drawdown(r.equity)
            row[f"{label} trades"] = len(r.trades)
            bench[label] = stats(r.benchmark)
        rows.append(row)

    df = pd.DataFrame(rows)
    ok = df[df["tune drop"] >= MAX_DROP].sort_values("tune /yr", ascending=False)
    years = "   ".join(f"{k.upper()} = {v[0][:4]}-{v[1][:4]}" for k, v in PERIODS.items())
    print(f"\n{len(df)} combinations tried; {len(ok)} kept their TUNE worst drop within {MAX_DROP:.0%}.")
    print(f"{years}   (ranked by TUNE return; CHECK and OLD played no part)\n")

    shown = ok.head(SHOW).copy()
    for c in shown.columns:
        if c.endswith("/yr") or c.endswith("drop"):
            shown[c] = shown[c].map(lambda v: f"{v:+.1%}")
    print(shown.to_string(index=False))

    def line(name, per_period):
        print(f"{name:26}" + "   ".join(f"{k.upper()} {v[0]:+.1%}/yr, drop {v[1]:+.1%}"
                                       for k, v in per_period.items()))

    print("\nNo-trading yardsticks:")
    line(f"Buy & hold {base.benchmark}", bench)
    for w in MIX_WEIGHTS:
        line(f"{w:.0%} {base.benchmark} / {1 - w:.0%} SHY mix",
             {k: stats(mix(data[base.benchmark], data["SHY"], w, *v)) for k, v in PERIODS.items()})

    if not ok.empty:
        pick = ok.iloc[0]
        print(f"\nPick: park in {pick['park']}, {pick['size']} positions, buy {pick['dip']} dips, exit {pick['exit']}.")
        for label in ("check", "old"):
            print(f"  {label.upper()} years: {pick[f'{label} /yr']:+.1%}/yr, worst drop {pick[f'{label} drop']:+.1%}")


if __name__ == "__main__":
    main()
