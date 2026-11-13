"""Find the settings that earn the most while keeping losses small.

    python compare.py

How it avoids fooling itself:
  1. Every combination in GRID is run on the TUNE years only.
  2. Combinations whose worst drop in the TUNE years was worse than MAX_DROP are thrown out.
  3. The rest are ranked by return per year in the TUNE years, and the top one is picked.
  4. Only then are the CHECK years shown. Those years played no part in the pick,
     so they're closer to what you'd see going forward.
"""

from dataclasses import replace
from itertools import product

import pandas as pd

from stonks.backtest import run_backtest
from stonks.config import BacktestConfig, Rules
from stonks.data import load_prices
from stonks.report import cagr, max_drawdown, mix, worst_year

TUNE = ("2015-01-01", "2020-12-31")
CHECK = ("2021-01-01", "2025-12-31")
MAX_DROP = -0.10   # the most you're willing to see the account fall from a high
SHOW = 12
MIX_WEIGHTS = [0.20, 0.30]   # stock/bond mixes to compare against: no trading at all

GRID = {
    "park_in": [None, "BIL", "SHY"],
    "max_position_pct": [0.10, 0.15, 0.20, 0.25],
    "dip_pct": [0.03, 0.04],
    "take_profit_pct": [0.06, 0.10],
}


def stats(series: pd.Series) -> tuple[float, float, float]:
    return cagr(series), max_drawdown(series), worst_year(series)[1]


def main() -> None:
    base = BacktestConfig()
    warmup = "2014-01-01"
    tickers = set(base.universe) | {base.benchmark, "SHY"} | {p for p in GRID["park_in"] if p}
    data = {t: load_prices(t, warmup, CHECK[1]) for t in sorted(tickers)}

    def run(rules: Rules, period):
        cfg = BacktestConfig(start=period[0], end=period[1], rules=rules)
        prices = {t: data[t].loc[:period[1]] for t in base.universe}
        park = data[rules.park_in].loc[:period[1]] if rules.park_in else None
        return run_backtest(prices, data[base.benchmark].loc[:period[1]], cfg, park_prices=park)

    rows, bench = [], {}
    for values in product(*GRID.values()):
        settings = dict(zip(GRID, values))
        rules = replace(Rules(), **settings)
        row = {
            "park": settings["park_in"] or "cash",
            "size": f"{settings['max_position_pct']:.0%}",
            "dip": f"{settings['dip_pct']:.0%}",
            "target": f"+{settings['take_profit_pct']:.0%}",
        }
        for label, period in (("tune", TUNE), ("check", CHECK)):
            r = run(rules, period)
            row[f"{label} /yr"] = cagr(r.equity)
            row[f"{label} drop"] = max_drawdown(r.equity)
            row[f"{label} worst yr"] = worst_year(r.equity)[1]
            bench[label] = (cagr(r.benchmark), max_drawdown(r.benchmark), worst_year(r.benchmark)[1])
        rows.append(row)

    df = pd.DataFrame(rows)
    ok = df[df["tune drop"] >= MAX_DROP].sort_values("tune /yr", ascending=False)
    print(f"\n{len(df)} combinations tried; {len(ok)} kept their TUNE worst drop within {MAX_DROP:.0%}.")
    print(f"TUNE = {TUNE[0][:4]}-{TUNE[1][:4]}   CHECK = {CHECK[0][:4]}-{CHECK[1][:4]}   "
          f"(ranked by TUNE return; CHECK played no part)\n")

    shown = ok.head(SHOW).copy()
    for c in shown.columns[4:]:
        shown[c] = shown[c].map(lambda v: f"{v:+.1%}")
    print(shown.to_string(index=False))

    def line(name, tune, check):
        print(f"{name:26}TUNE {tune[0]:+.1%}/yr, drop {tune[1]:+.1%}, worst yr {tune[2]:+.1%}   "
              f"CHECK {check[0]:+.1%}/yr, drop {check[1]:+.1%}, worst yr {check[2]:+.1%}")

    print("\nNo-trading yardsticks:")
    line(f"Buy & hold {base.benchmark}", bench["tune"], bench["check"])
    for w in MIX_WEIGHTS:
        line(f"{w:.0%} {base.benchmark} / {1 - w:.0%} SHY mix",
             *(stats(mix(data[base.benchmark], data["SHY"], w, *p)) for p in (TUNE, CHECK)))
    line("SHY alone", *(stats(data["SHY"]["Close"].loc[p[0]:p[1]]) for p in (TUNE, CHECK)))

    if not ok.empty:
        pick = ok.iloc[0]
        print(f"\nPick: park in {pick['park']}, {pick['size']} positions, buy {pick['dip']} dips, "
              f"sell at {pick['target']}.")
        print(f"  CHECK years: {pick['check /yr']:+.1%}/yr, worst drop {pick['check drop']:+.1%}, "
              f"worst year {pick['check worst yr']:+.1%}")


if __name__ == "__main__":
    main()
