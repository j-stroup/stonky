"""Download daily prices from Yahoo Finance and cache them as CSV files."""

from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent.parent / "data"


def load_prices(ticker: str, start: str, end: str, refresh: bool = False) -> pd.DataFrame:
    """Daily OHLCV bars, adjusted for splits and dividends, indexed by date."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{ticker}.csv"
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)

    df = None
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col="Date", parse_dates=True)
        # Re-download if the cache doesn't reach back far enough.
        if df.empty or df.index[0] > start_ts + pd.Timedelta(days=7):
            df = None

    if df is None:
        df = _download(ticker, start, end)
        df.to_csv(path, index_label="Date")

    return df.loc[start_ts:end_ts]


def load_recent(ticker: str, today: pd.Timestamp, calendar_days: int = 450) -> pd.DataFrame:
    """Fresh daily bars (not cached) for live use, ending with the last COMPLETE day.

    Today's bar is dropped even if Yahoo returns one, because it's still changing.
    """
    start = (today - pd.Timedelta(days=calendar_days)).strftime("%Y-%m-%d")
    df = _download(ticker, start, today.strftime("%Y-%m-%d"))
    return df[df.index < today.normalize()]


def _download(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    print(f"Downloading {ticker}...")
    # yfinance's end date is exclusive, so add a day.
    end_inclusive = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.Ticker(ticker).history(start=start, end=end_inclusive, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"No price data returned for {ticker}")
    df.index = df.index.tz_localize(None).normalize()
    df.index.name = "Date"
    return df[["Open", "High", "Low", "Close", "Volume"]]
