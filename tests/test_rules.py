import pandas as pd
import pytest

from stonks.backtest import run_backtest
from stonks.config import BacktestConfig, Rules
from stonks.strategy import Position, exit_reason, wants_to_buy

RULES = Rules(take_profit_pct=0.06, stop_loss_pct=0.05, slow_loss_pct=0.02,
              slow_loss_days=3, max_hold_days=10, slippage_pct=0.0)


def make_position(entry=100.0):
    return Position("TEST", shares=10, entry_price=entry, entry_date=pd.Timestamp("2024-01-02"))


# --- Exit rules ----------------------------------------------------------

def test_take_profit():
    assert exit_reason(make_position(), 106.0, RULES).startswith("take profit")


def test_stop_loss():
    assert exit_reason(make_position(), 95.0, RULES).startswith("stop loss")


def test_hold_inside_the_band():
    assert exit_reason(make_position(), 101.0, RULES) is None


def test_slow_loss_needs_consecutive_days():
    pos = make_position()
    for close in [97.5, 97.5, 101.0, 97.5, 97.5]:   # the recovery resets the count
        pos.update(close, RULES)
    assert exit_reason(pos, 97.5, RULES) is None
    pos.update(97.5, RULES)                         # third day in a row
    assert exit_reason(pos, 97.5, RULES).startswith("slow loss")


def test_max_hold():
    pos = make_position()
    for _ in range(10):
        pos.update(100.5, RULES)
    assert exit_reason(pos, 100.5, RULES).startswith("max hold")


# --- Entry rule ----------------------------------------------------------

def test_buy_on_dip_in_uptrend():
    assert wants_to_buy(close=95, sma_fast=100, sma_trend=90, rules=Rules(dip_pct=0.04))


def test_no_buy_on_small_dip():
    assert not wants_to_buy(close=98, sma_fast=100, sma_trend=90, rules=Rules(dip_pct=0.04))


def test_no_buy_in_downtrend():
    assert not wants_to_buy(close=95, sma_fast=100, sma_trend=120, rules=Rules(dip_pct=0.04))


def test_no_buy_without_history():
    assert not wants_to_buy(close=95, sma_fast=float("nan"), sma_trend=90, rules=Rules())


# --- Engine --------------------------------------------------------------

def bars(closes, opens=None, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    opens = opens or closes
    return pd.DataFrame({"Open": opens, "High": closes, "Low": closes,
                         "Close": closes, "Volume": 1000}, index=idx)


def test_engine_fills_at_next_open_and_takes_profit():
    # Flat at 100, dips to 90 (buy signal at that close), then rallies.
    closes = [100] * 5 + [90, 91, 98, 100]
    opens = [100] * 5 + [90, 92, 95, 99]
    rules = Rules(fast_sma_days=5, trend_sma_days=5, require_uptrend=False, dip_pct=0.05,
                  take_profit_pct=0.06, max_position_pct=0.5, slippage_pct=0.0,
                  park_in=None)
    cfg = BacktestConfig(start="2024-01-01", end="2024-12-31", starting_cash=1000,
                         universe=["TEST"], rules=rules)
    result = run_backtest({"TEST": bars(closes, opens)}, bars(closes, opens), cfg)

    assert len(result.trades) == 1
    trade = result.trades[0]
    # Signal on the 90 close -> bought at the NEXT day's open (92), not at 90.
    assert trade.entry_price == 92
    # 98 close is +6.5% -> sell signal -> sold at the next open (99).
    assert trade.reason.startswith("take profit")
    assert trade.exit_price == 99
    assert trade.shares == 5          # 50% of $1000 / $92 = 5 whole shares
    assert trade.pnl == pytest.approx(5 * (99 - 92))
    assert not result.open_positions


def test_engine_never_spends_more_than_it_has():
    closes = [100] * 5 + [90] * 20
    rules = Rules(fast_sma_days=5, trend_sma_days=5, require_uptrend=False, dip_pct=0.05,
                  max_position_pct=1.0, slippage_pct=0.0, park_in=None)
    cfg = BacktestConfig(start="2024-01-01", end="2024-12-31", starting_cash=1000,
                         universe=["A", "B"], rules=rules)
    data = {"A": bars(closes), "B": bars(closes)}
    result = run_backtest(data, bars(closes), cfg)
    held = sum(p.shares * 90 for p in result.open_positions)
    assert held <= 1000
    assert result.equity.iloc[-1] == pytest.approx(1000, abs=100)


# --- Idle-cash parking ---------------------------------------------------

def test_idle_cash_tracks_benchmark_when_no_trades():
    # Prices never dip, so no trades: the account should just follow SPY.
    spy = bars([100, 101, 102, 103, 104, 105, 110], opens=[100, 101, 102, 103, 104, 105, 110])
    flat = bars([50] * 7)
    rules = Rules(fast_sma_days=5, trend_sma_days=5, require_uptrend=False, slippage_pct=0.0,
                  park_in="SPY")
    cfg = BacktestConfig(start="2024-01-01", end="2024-12-31", starting_cash=1000,
                         universe=["FLAT"], rules=rules)
    result = run_backtest({"FLAT": flat}, spy, cfg)
    assert not result.trades
    assert result.equity.iloc[-1] == pytest.approx(result.benchmark.iloc[-1])
    assert result.equity.iloc[-1] == pytest.approx(1100)


def test_parked_shares_are_sold_to_fund_a_buy():
    closes = [100] * 5 + [90, 91, 98, 100]
    opens = [100] * 5 + [90, 92, 95, 99]
    spy = bars([200] * 9)
    rules = Rules(fast_sma_days=5, trend_sma_days=5, require_uptrend=False, dip_pct=0.05,
                  take_profit_pct=0.06, max_position_pct=0.5, slippage_pct=0.0, park_in="SPY")
    cfg = BacktestConfig(start="2024-01-01", end="2024-12-31", starting_cash=1000,
                         universe=["TEST"], rules=rules)
    result = run_backtest({"TEST": bars(closes, opens)}, spy, cfg)

    assert any("UNPARK" in line for line in result.log)
    assert len(result.trades) == 1 and result.trades[0].shares == 5
    # Profit from the trade (5 * $7) on top of the flat SPY parking.
    assert result.equity.iloc[-1] == pytest.approx(1035)
    assert result.parked.iloc[-1] > 0.9


def test_parks_in_a_separate_ticker():
    # Cash parked in a "bond" that rises 1% while SPY (the benchmark) falls.
    bond = bars([100, 100.5, 101])
    spy = bars([100, 90, 80])
    cfg = BacktestConfig(start="2024-01-01", end="2024-12-31", starting_cash=1000,
                         universe=["FLAT"], rules=Rules(park_in="BOND", slippage_pct=0.0))
    result = run_backtest({"FLAT": bars([50] * 3)}, spy, cfg, park_prices=bond)
    assert result.equity.iloc[-1] == pytest.approx(1010)
    assert result.benchmark.iloc[-1] == pytest.approx(800)


def test_missing_park_prices_is_an_error():
    cfg = BacktestConfig(start="2024-01-01", end="2024-12-31", universe=["FLAT"],
                         rules=Rules(park_in="BOND"))
    with pytest.raises(ValueError):
        run_backtest({"FLAT": bars([50] * 3)}, bars([100] * 3), cfg)


# --- Trailing stop (optional) --------------------------------------------

TRAIL = Rules(trail_after_pct=0.06, trail_pct=0.03, stop_loss_pct=0.05, max_hold_days=10,
              slow_loss_pct=0.02, slow_loss_days=3)


def ride(closes, rules=TRAIL):
    """Replay closes for a position bought at 100; return the first exit reason and its day."""
    pos = make_position(100.0)
    for day, close in enumerate(closes, 1):
        pos.update(close, rules)
        reason = exit_reason(pos, close, rules)
        if reason:
            return day, reason
    return None, None


def test_trailing_lets_a_winner_run_past_the_take_profit():
    day, reason = ride([103, 107, 112, 120, 125, 121])     # +7% no longer sells
    assert day == 6 and reason.startswith("trailing stop (+21.0%, best was +25.0%)")


def test_trailing_does_not_start_before_the_threshold():
    assert ride([103, 105, 102, 104]) == (None, None)


def test_trailing_keeps_the_stop_loss():
    day, reason = ride([98, 96, 94.5])
    assert day == 3 and reason.startswith("stop loss")


def test_max_hold_does_not_cut_a_running_winner():
    day, reason = ride([101] * 3 + [107, 108, 109, 110, 111, 112, 113, 114])
    assert reason is None


def test_max_hold_still_applies_before_it_runs():
    day, reason = ride([101] * 12)
    assert day == 10 and reason.startswith("max hold")


def test_default_rules_still_use_fixed_take_profit():
    day, reason = ride([103, 107], rules=Rules())
    assert day == 2 and reason.startswith("take profit")
