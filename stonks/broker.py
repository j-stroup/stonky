"""Where orders go. The trading routine only talks to the Broker interface,
so it can't tell (and doesn't care) whether money is real, paper or simulated.

  SimBroker          - a pretend account saved in a local JSON file. No keys needed.
  AlpacaPaperBroker  - Alpaca's paper-trading account. Refuses live keys.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class Account:
    cash: float
    equity: float


@dataclass
class Holding:
    qty: int
    avg_price: float


@dataclass
class Fill:
    ticker: str
    side: str          # "buy" or "sell"
    qty: int
    price: float       # average fill price; 0 if nothing filled
    status: str        # "filled", "partially_filled", "rejected", "timeout", ...


class Broker(Protocol):
    name: str

    def market_is_open(self) -> bool: ...
    def account(self) -> Account: ...
    def holdings(self) -> dict[str, Holding]: ...
    def traded_today(self, today) -> bool:
        """True if this bot already placed orders today (from any computer)."""
        ...
    def market_orders(self, orders: list[tuple[str, str, int]]) -> list[Fill]:
        """Submit (ticker, side, qty) market orders and wait until they finish."""
        ...


# --- Local simulation ----------------------------------------------------

class SimBroker:
    """A pretend account kept in a JSON file. Fills at the prices you give it."""

    name = "sim"

    def __init__(self, path: Path, prices: dict[str, float], starting_cash: float = 10_000.0,
                 slippage_pct: float = 0.0005):
        self.path = path
        self.prices = prices
        self.slippage_pct = slippage_pct
        if path.exists():
            raw = json.loads(path.read_text())
            self.cash = raw["cash"]
            self.positions = {t: Holding(**h) for t, h in raw["positions"].items()}
        else:
            self.cash = starting_cash
            self.positions = {}

    def market_is_open(self) -> bool:
        return True

    def traded_today(self, today) -> bool:
        return False   # the sim account only lives on this computer; state.json covers it

    def account(self) -> Account:
        value = sum(h.qty * self.prices.get(t, h.avg_price) for t, h in self.positions.items())
        return Account(cash=self.cash, equity=self.cash + value)

    def holdings(self) -> dict[str, Holding]:
        return {t: Holding(h.qty, h.avg_price) for t, h in self.positions.items()}

    def market_orders(self, orders):
        fills = []
        for ticker, side, qty in orders:
            base = self.prices[ticker]
            if side == "buy":
                price = base * (1 + self.slippage_pct)
                if qty * price > self.cash:
                    fills.append(Fill(ticker, side, 0, 0.0, "rejected"))
                    continue
                self.cash -= qty * price
                old = self.positions.get(ticker, Holding(0, 0.0))
                total = old.qty + qty
                self.positions[ticker] = Holding(total, (old.qty * old.avg_price + qty * price) / total)
            else:
                held = self.positions.get(ticker)
                if not held or held.qty < qty:
                    fills.append(Fill(ticker, side, 0, 0.0, "rejected"))
                    continue
                price = base * (1 - self.slippage_pct)
                self.cash += qty * price
                held.qty -= qty
                if held.qty == 0:
                    del self.positions[ticker]
            fills.append(Fill(ticker, side, qty, price, "filled"))
        self._save()
        return fills

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "cash": self.cash,
            "positions": {t: {"qty": h.qty, "avg_price": h.avg_price} for t, h in self.positions.items()},
        }, indent=2))


# --- Alpaca paper trading ------------------------------------------------

def load_env(path: Path) -> None:
    """Read KEY=value lines from a .env file into the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


ORDER_ID_PREFIX = "stonks-"


class AlpacaPaperBroker:
    name = "alpaca-paper"

    def __init__(self, api_key: str | None = None, secret_key: str | None = None,
                 fill_timeout_s: float = 120.0):
        from alpaca.trading.client import TradingClient

        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in the .env file")
        # Alpaca paper keys start with "PK"; live keys don't. Never touch a live account here.
        if not api_key.startswith("PK"):
            raise RuntimeError("That doesn't look like a PAPER key (paper keys start with 'PK'). Refusing to run.")
        self.api_key, self.secret_key = api_key, secret_key
        self.client = TradingClient(api_key, secret_key, paper=True)
        self.fill_timeout_s = fill_timeout_s

    def market_is_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def traded_today(self, today) -> bool:
        """Look in the Alpaca account itself, so two computers can't both trade the same day."""
        import pandas as pd
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        midnight = pd.Timestamp(today).normalize().tz_localize("America/New_York").to_pydatetime()
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=midnight, limit=500))
        return any((o.client_order_id or "").startswith(ORDER_ID_PREFIX) for o in orders)

    def account(self) -> Account:
        a = self.client.get_account()
        if a.trading_blocked or a.account_blocked:
            raise RuntimeError(f"Alpaca account is blocked (status: {a.status})")
        return Account(cash=float(a.cash), equity=float(a.equity))

    def holdings(self) -> dict[str, Holding]:
        return {p.symbol: Holding(int(float(p.qty)), float(p.avg_entry_price))
                for p in self.client.get_all_positions()}

    def market_orders(self, orders):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        submitted = []
        for ticker, side, qty in orders:
            try:
                order = self.client.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=qty, time_in_force=TimeInForce.DAY,
                    client_order_id=f"{ORDER_ID_PREFIX}{uuid.uuid4().hex[:20]}",
                    side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                ))
                submitted.append((ticker, side, qty, order.id))
            except Exception as e:  # rejected up front (e.g. not enough buying power)
                submitted.append((ticker, side, qty, None))
                print(f"  order {side} {qty} {ticker} rejected: {e}")

        done_states = {"filled", "canceled", "expired", "rejected", "done_for_day"}
        deadline = time.time() + self.fill_timeout_s
        fills: dict[int, Fill] = {}
        while True:
            for i, (ticker, side, qty, oid) in enumerate(submitted):
                if i in fills:
                    continue
                if oid is None:
                    fills[i] = Fill(ticker, side, 0, 0.0, "rejected")
                    continue
                o = self.client.get_order_by_id(oid)
                status = str(o.status.value if hasattr(o.status, "value") else o.status)
                if status in done_states:
                    fills[i] = Fill(ticker, side, int(float(o.filled_qty or 0)),
                                    float(o.filled_avg_price or 0), status)
            if len(fills) == len(submitted) or time.time() > deadline:
                break
            time.sleep(2)

        result = []
        for i, (ticker, side, qty, oid) in enumerate(submitted):
            if i in fills:
                result.append(fills[i])
            else:
                o = self.client.get_order_by_id(oid)
                result.append(Fill(ticker, side, int(float(o.filled_qty or 0)),
                                   float(o.filled_avg_price or 0), "timeout"))
        return result
