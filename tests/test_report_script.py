import pandas as pd

from report import FillRow, PositionRow, Snapshot, format_report, realized_trades
from stonks.live import State

D = pd.Timestamp


def fills():
    return [
        FillRow(D("2026-11-02"), "buy", "XLE", 10, 60.0),
        FillRow(D("2026-11-02"), "buy", "SHY", 90, 80.0),
        FillRow(D("2026-11-05"), "sell", "XLE", 10, 64.0),   # +40
        FillRow(D("2026-11-06"), "buy", "XLK", 5, 100.0),
        FillRow(D("2026-11-10"), "sell", "XLK", 5, 94.0),    # -30
        FillRow(D("2026-11-11"), "buy", "XLF", 20, 50.0),
    ]


def test_realized_trades_uses_average_cost_and_skips_park():
    assert realized_trades(fills(), skip={"SHY"}) == [40.0, -30.0]


def test_report_shows_the_essentials():
    snap = Snapshot(
        equity=10_150.0, cash=12.0, start_value=10_000.0, start_date="2026-11-02",
        positions=[PositionRow("XLF", 20, 50.0, 51.0, 20.0, 0.02),
                   PositionRow("SHY", 90, 80.0, 80.5, 45.0, 0.00625)],
        fills=fills(), state=State(entries={"XLF": "2026-11-11"}, last_run="2026-11-13"),
    )
    text = format_report(snap, park="SHY", today=D("2026-11-13"))
    assert "$10,150.00   +150.00 (+1.50%)" in text
    assert "last ran 2026-11-13" in text
    assert "held 2 trading days" in text
    assert "2 (1 won, 1 lost), realized +10.00" in text
    assert text.index("XLF") < text.index("SHY")          # trades listed before idle cash
    assert "HALTED" not in text


def test_report_flags_halt_and_stop_file():
    snap = Snapshot(10_000, 10_000, 10_000, "", [], [], State(halted=True, halt_reason="fell 13%"),
                    problems=["ERROR loading prices"], stop_file=True)
    text = format_report(snap, park="SHY", today=D("2026-11-13"))
    assert "HALTED: fell 13%" in text
    assert "STOP file present" in text
    assert "ERROR loading prices" in text
    assert "(none yet)" in text
