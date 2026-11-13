"""Turn a backtest result into numbers a person can judge."""

from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from stonks.backtest import BacktestResult


def cagr(series: pd.Series) -> float:
    years = (series.index[-1] - series.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    return (series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1


def max_drawdown(series: pd.Series) -> float:
    """Largest drop from a previous high, as a negative fraction."""
    return (series / series.cummax() - 1).min()


def worst_year(series: pd.Series) -> tuple[int, float]:
    """The calendar year with the lowest return, as (year, return)."""
    yearly = series.groupby(series.index.year).last()
    first = pd.Series([series.iloc[0]], index=[yearly.index[0] - 1])
    returns = pd.concat([first, yearly]).pct_change().dropna()
    return int(returns.idxmin()), float(returns.min())


def summarize(result: BacktestResult) -> dict:
    eq, bm, trades = result.equity, result.benchmark, result.trades
    start_cash = result.config.starting_cash
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    return {
        "start_value": start_cash,
        "end_value": eq.iloc[-1],
        "total_return": eq.iloc[-1] / start_cash - 1,
        "cagr": cagr(eq),
        "max_drawdown": max_drawdown(eq),
        "benchmark_end_value": bm.iloc[-1],
        "benchmark_total_return": bm.iloc[-1] / start_cash - 1,
        "benchmark_cagr": cagr(bm),
        "benchmark_max_drawdown": max_drawdown(bm),
        "worst_year": worst_year(eq),
        "benchmark_worst_year": worst_year(bm),
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win": sum(t.return_pct for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(t.return_pct for t in losses) / len(losses) if losses else 0.0,
        "avg_days_held": sum(t.days_held for t in trades) / len(trades) if trades else 0.0,
        "avg_invested": result.exposure.mean(),
        "avg_parked": result.parked.mean(),
        "exit_reasons": Counter(t.reason.split(" (")[0].split(":")[0] for t in trades),
        "open_positions": len(result.open_positions),
    }


def mix(stock: pd.DataFrame, bond: pd.DataFrame, weight: float, start, end) -> pd.Series:
    """Growth of $1 in a fixed stock/bond mix, rebalanced daily (an idealized, cost-free yardstick)."""
    daily = weight * stock["Close"].pct_change() + (1 - weight) * bond["Close"].pct_change()
    return (1 + daily.loc[start:end].fillna(0)).cumprod()


def print_summary(result: BacktestResult, yardstick: pd.Series | None = None,
                  yardstick_name: str = "") -> None:
    """yardstick: growth of $1 in a no-trading alternative with similar risk (e.g. a stock/bond mix)."""
    s = summarize(result)
    cfg = result.config
    print()
    print(f"Backtest {cfg.start} to {cfg.end}  |  {', '.join(cfg.universe)}")
    print("-" * 60)
    print(f"{'':24}{'Strategy':>16}{'Buy & hold ' + cfg.benchmark:>20}")
    print(f"{'End value':24}{s['end_value']:>16,.2f}{s['benchmark_end_value']:>20,.2f}")
    print(f"{'Total return':24}{s['total_return']:>16.1%}{s['benchmark_total_return']:>20.1%}")
    print(f"{'Per year (CAGR)':24}{s['cagr']:>16.1%}{s['benchmark_cagr']:>20.1%}")
    print(f"{'Worst drop (drawdown)':24}{s['max_drawdown']:>16.1%}{s['benchmark_max_drawdown']:>20.1%}")
    (y, r), (by, br) = s["worst_year"], s["benchmark_worst_year"]
    print(f"{'Worst calendar year':24}{f'{r:.1%} ({y})':>16}{f'{br:.1%} ({by})':>20}")
    print("-" * 60)
    print(f"Trades: {s['trades']}   Win rate: {s['win_rate']:.0%}   "
          f"Avg win: {s['avg_win']:+.1%}   Avg loss: {s['avg_loss']:+.1%}")
    print(f"Avg days held: {s['avg_days_held']:.0f}   "
          f"Still open: {s['open_positions']}")
    print(f"Avg % of account: {s['avg_invested']:.0%} in trades, "
          f"{s['avg_parked']:.0%} parked in {cfg.rules.park_in or 'nothing'}, "
          f"{1 - s['avg_invested'] - s['avg_parked']:.0%} cash")
    print("Why positions were sold: " + ", ".join(
        f"{reason} {count}" for reason, count in s["exit_reasons"].most_common()))
    print()
    if yardstick is not None:
        y = yardstick.reindex(result.equity.index).ffill() * cfg.starting_cash
        ywy = worst_year(y)
        print(f"No-trading yardstick, {yardstick_name}: ends at ${y.iloc[-1]:,.2f}, "
              f"{cagr(y):.1%}/yr, worst drop {max_drawdown(y):.1%}, worst year {ywy[1]:.1%} ({ywy[0]})")
        more = s["end_value"] > y.iloc[-1]
        smaller = s["max_drawdown"] > max_drawdown(y)
        print(f"Against it, the strategy earned {'MORE' if more else 'LESS'} "
              f"with a {'SMALLER' if smaller else 'BIGGER'} worst drop.")
    verdict = "beat" if s["end_value"] > s["benchmark_end_value"] else "did not beat"
    print(f"Against buy & hold {cfg.benchmark}, the strategy {verdict} it on return.")


def save_results(result: BacktestResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(t) for t in result.trades]).to_csv(out_dir / "trades.csv", index=False)
    pd.concat([result.equity, result.benchmark, result.exposure, result.parked], axis=1).to_csv(
        out_dir / "equity.csv", index_label="Date")
    (out_dir / "log.txt").write_text("\n".join(result.log), encoding="utf-8")
    print(f"Saved trades.csv, equity.csv and log.txt to {out_dir}")
