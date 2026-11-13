import pandas as pd
import pytest

from stonks.broker import AlpacaPaperBroker, Holding, SimBroker
from stonks.config import Rules, Safety
from stonks.live import Journal, State, check_bars, plan_day, run_day
from stonks.strategy import add_indicators

RULES = Rules(fast_sma_days=5, trend_sma_days=5, require_uptrend=False, dip_pct=0.05,
              take_profit_pct=0.06, stop_loss_pct=0.05, max_position_pct=0.10,
              slippage_pct=0.0, park_in="PARK")
SAFETY = Safety(price_buffer_pct=0.0)
TODAY = pd.Timestamp("2024-01-15")   # a Monday; bars end the Friday before


def bars(closes, end="2024-01-12"):
    idx = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": 1000}, index=idx)


def market(dip_ticker_closes):
    return {"DIP": bars(dip_ticker_closes), "FLAT": bars([50.0] * 8), "PARK": bars([100.0] * 8)}


def last_prices(raw):
    return {t: df["Close"].iloc[-1] for t, df in raw.items()}


def run(broker, raw, state, today=TODAY, safety=SAFETY):
    return run_day(broker, raw, ["DIP", "FLAT"], RULES, safety, state, today, Journal(None))


# --- Deciding ------------------------------------------------------------

def test_plan_sells_at_take_profit_and_buys_dips():
    raw = market([100.0] * 7 + [90.0])
    b = {t: add_indicators(df, RULES) for t, df in raw.items()}
    holdings = {"FLAT": Holding(10, 47.0)}            # 50 vs 47 = +6.4%
    plan = plan_day(b, holdings, {"FLAT": "2024-01-08"}, ["DIP", "FLAT"], RULES)
    assert plan.sells == [("FLAT", 10, "take profit (+6.4%)")]
    assert plan.buys == ["DIP"]


def test_plan_rebuilds_slow_loss_counter_from_history():
    rules = Rules(slow_loss_pct=0.02, slow_loss_days=3, fast_sma_days=5, trend_sma_days=5)
    raw = {"A": bars([100, 100, 100, 100, 97.5, 97.5, 97.5, 97.5])}
    b = {t: add_indicators(df, rules) for t, df in raw.items()}
    plan = plan_day(b, {"A": Holding(1, 100.0)}, {"A": str(raw["A"].index[4].date())}, ["A"], rules)
    assert plan.sells and plan.sells[0][2].startswith("slow loss")


def test_unmanaged_holdings_are_never_touched():
    raw = market([100.0] * 8)
    b = {t: add_indicators(df, RULES) for t, df in raw.items()}
    plan = plan_day(b, {"TSLA": Holding(5, 1.0)}, {}, ["DIP", "FLAT"], RULES)
    assert not plan.sells


def test_refuses_stale_data():
    raw = {"A": bars([1.0] * 8, end="2024-01-02")}
    with pytest.raises(RuntimeError, match="stale"):
        check_bars(raw, TODAY, SAFETY)


def test_refuses_todays_unfinished_bar():
    raw = {"A": bars([1.0] * 8, end="2024-01-15")}
    with pytest.raises(RuntimeError, match="unfinished"):
        check_bars(raw, TODAY, SAFETY)


# --- Doing ---------------------------------------------------------------

def test_first_day_buys_dip_and_parks_the_rest(tmp_path):
    raw = market([100.0] * 7 + [90.0])
    broker = SimBroker(tmp_path / "acct.json", last_prices(raw), 10_000, slippage_pct=0.0)
    state = State()
    run(broker, raw, state)

    h = broker.holdings()
    assert h["DIP"].qty == 11                 # 10% of $10,000 at $90
    assert h["PARK"].qty == 90                # the remaining ~$9,010 at $100
    assert broker.account().cash < 100
    assert state.entries == {"DIP": "2024-01-15"}


def test_next_day_take_profit_sells_and_reparks(tmp_path):
    raw = market([100.0] * 7 + [90.0])
    broker = SimBroker(tmp_path / "acct.json", last_prices(raw), 10_000, slippage_pct=0.0)
    state = State()
    run(broker, raw, state)

    # A day later DIP has rallied to 96 (+6.7%).
    raw2 = {t: bars(list(df["Close"].iloc[1:]) + [96.0 if t == "DIP" else df["Close"].iloc[-1]],
                    end="2024-01-15") for t, df in raw.items()}
    broker.prices = last_prices(raw2)
    run(broker, raw2, state, today=pd.Timestamp("2024-01-16"))

    h = broker.holdings()
    assert "DIP" not in h
    assert h["PARK"].qty == 100               # $9,000 + 11 * $96 back in PARK
    assert state.entries == {}


def test_buys_are_paid_for_by_selling_parked_shares(tmp_path):
    raw = market([100.0] * 7 + [90.0])
    broker = SimBroker(tmp_path / "acct.json", last_prices(raw), 0, slippage_pct=0.0)
    broker.positions = {"PARK": Holding(100, 100.0)}  # fully parked, no cash
    run(broker, raw, State())
    h = broker.holdings()
    assert h["DIP"].qty == 11
    assert h["PARK"].qty < 100


def test_halt_stops_buys_but_still_sells(tmp_path):
    raw = market([100.0] * 7 + [90.0])
    broker = SimBroker(tmp_path / "acct.json", last_prices(raw), 8_000, slippage_pct=0.0)
    broker.positions = {"FLAT": Holding(10, 60.0)}     # 50 vs 60: stop loss
    state = State(peak_equity=10_000, entries={"FLAT": "2024-01-08"})
    run(broker, raw, state)

    assert state.halted
    h = broker.holdings()
    assert "FLAT" not in h            # the stop loss still ran
    assert "DIP" not in h             # but no new buy
    assert "PARK" not in h            # and no parking


def test_dry_run_places_no_orders(tmp_path):
    raw = market([100.0] * 7 + [90.0])
    broker = SimBroker(tmp_path / "acct.json", last_prices(raw), 10_000)
    plan = run_day(broker, raw, ["DIP", "FLAT"], RULES, SAFETY, State(), TODAY, Journal(None), dry_run=True)
    assert plan.buys == ["DIP"]
    assert broker.holdings() == {}
    assert not (tmp_path / "acct.json").exists()


def test_alpaca_refuses_live_keys():
    with pytest.raises(RuntimeError, match="PAPER"):
        AlpacaPaperBroker(api_key="AKLIVEKEY", secret_key="x")
