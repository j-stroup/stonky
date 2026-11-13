# stonks

A rule-based trader that acts only on numbers. Stage 1 is a **backtester**: it replays the rules over past prices with pretend money and compares the result with buying and holding SPY.

Plan: **backtest → paper trade → small live account**, using the same strategy code at every stage.

## Setup

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```
.venv\Scripts\python.exe run_backtest.py
.venv\Scripts\python.exe run_backtest.py --start 2018-01-01 --take-profit 0.08 --stop-loss 0.04
.venv\Scripts\python.exe run_backtest.py --tickers XLK XLV XLP --cash 5000
.venv\Scripts\python.exe run_backtest.py --no-park --position-size 0.20 --dip 0.03
.venv\Scripts\python.exe compare.py
.venv\Scripts\python.exe -m pytest
```

`compare.py` tries 48 combinations on 2015–2020 (tune) and drops any whose worst drop was worse than -10% (`MAX_DROP`). It picks the best return among the rest, then shows how they all did on 2021–2025 (check), which played no part in the pick. It also prints yardsticks that involve no trading at all, such as a fixed 20% SPY / 80% SHY mix.

Prices are downloaded from Yahoo Finance once and cached in `data/`. Pass `--refresh` to download them again. Each run writes these files to `results/`:
- `trades.csv`: every round trip, with the reason it was sold
- `equity.csv`: account value each day, next to the benchmark
- `log.txt`: every buy and sell in plain English

## Paper trading

`paper_trade.py` runs the same rules once a day. Run it shortly after the market opens, between 9:30 AM and 4:00 PM Eastern. It decides using yesterday's closing prices, sells first, sells parked SHY if buys need cash, buys, and then parks the leftover cash. That's the same order the backtest uses.

```
.venv\Scripts\python.exe paper_trade.py sim --dry-run   # show today's plan, no orders
.venv\Scripts\python.exe paper_trade.py sim             # pretend $10k account saved in state/sim/
.venv\Scripts\python.exe paper_trade.py alpaca --dry-run
.venv\Scripts\python.exe paper_trade.py alpaca          # Alpaca paper account
```

**`sim` mode** needs no account or keys. It fills at the last close, so it's for trying things out. **`alpaca` mode** places real orders in Alpaca's paper account, with real fills and fake money.

### Alpaca setup (one time)

1. Sign up at alpaca.markets, open the dashboard, and switch to **Paper Trading**.
2. Generate API keys there. Paper keys start with `PK`.
3. Copy `.env.example` to `.env` and paste in the keys. `.env` is git-ignored, so the keys stay off GitHub if you push this.
4. Optional: to match the backtest's $10,000, open a new paper account. Click the paper account number in the top-left corner, choose **Open New Paper Account**, and set the balance. Alpaca no longer resets existing accounts. A new account comes with **new API keys**, so update `.env`.

### Run it every trading day

Use Windows Task Scheduler. Pick a time about 15 minutes after the 9:30 AM Eastern open, in your local time zone. On holidays, alpaca mode sees that the market is closed and exits.

```
schtasks /create /tn "stonks paper" /sc weekly /d MON,TUE,WED,THU,FRI /st 09:45 /tr "\"C:\path\to\stonks\.venv\Scripts\python.exe\" \"C:\path\to\stonks\paper_trade.py\" alpaca"
```

### Safety

- **Kill switch:** create a file named `STOP` in this folder and nothing trades until you delete it.
- **Drawdown halt:** if the account falls 12% from its best value, it stops buying. Sells still run, so stop losses keep working. It stays stopped until you run with `--reset-halt`. The backtest's worst drop was about 10%, so a bigger one means something is behaving differently from what was tested.
- It only trades the ETFs in `config.py` and SHY. Anything else in the account is left alone.
- It refuses to trade if the price data is stale or includes today's unfinished bar.
- It only runs once per day. Use `--force` to run it again.
- It only accepts paper keys.

Every decision and fill is written to `state/<mode>/journal.log`, and account value is logged to `state/<mode>/equity_history.csv` each day.

## The rules (edit `stonks/config.py`)

| Rule | Default |
|---|---|
| Sell when up | +6% |
| Sell immediately when down | -5% |
| Sell after a slow bleed | at or below -2% for 10 trading days in a row |
| Sell after holding | 60 trading days |
| Buy when | price is 3% below its 20-day average *and* above its 200-day average |
| Position size | at most 10% of the account, at most 8 positions |
| Idle cash | parked in SHY (short-term Treasuries), sold automatically to fund new buys (`--park BIL`, `--park SPY`, or `--no-park`) |
| Costs | 0.05% slippage on each fill |

Decisions use the day's closing price. Orders are filled at the **next day's open**, so the backtest never uses prices it couldn't have known.

## Files

- `stonks/config.py`: all the numbers
- `stonks/strategy.py`: the buy and sell rules (shared by every stage)
- `stonks/backtest.py`: the day-by-day simulation
- `stonks/data.py`: price download and cache
- `stonks/report.py`: summary stats and CSV output
- `compare.py`: tune-then-check comparison of several settings
- `paper_trade.py`: the daily paper-trading command
- `stonks/live.py`: the daily routine (decide, sell, buy, park) and safety stops
- `stonks/broker.py`: the sim account and the Alpaca paper account behind one interface
- `tests/`: checks that each rule fires when it should

## What the backtests showed so far

| 2015–2025, $10k | Per year | Worst drop |
|---|---|---|
| Rules, idle cash kept as cash | 2.6% | -6.4% |
| Rules, idle cash parked in SPY | 13.8% | -33.3% |
| Buy & hold SPY | 13.3% | -33.5% |

- Parking idle cash fixes the gap with SPY, but the account is then about 90% SPY, so it drops as much as SPY does in a crash.
- The trading rules on their own add little either way. In the 2021–2025 check, the best settings from 2015–2020 earned 13.1%/yr against SPY's 14.4%.
- Returns and drawdowns trade off against each other. Kept in cash with 20% positions, the rules made about 3–8%/yr with drops under 15%.

**Goal chosen: small losses.** Current defaults (idle cash in SHY, 10% positions, 3% dips, +6% target):

| 2015–2025, $10k | Per year | Worst drop | Worst year |
|---|---|---|---|
| These rules | 4.4% | -9.8% | -3.6% (2022) |
| 20% SPY / 80% SHY, no trading | 4.1% | -8.7% | -6.5% (2022) |
| Buy & hold SPY | 13.3% | -33.5% | -18.1% (2022) |

On 2021–2025, years the pick was never tuned on, the rules made 5.0%/yr with a -6.4% worst drop. The plain 20/80 mix made 4.4%/yr with -8.7%. So the rules came out slightly ahead of a simple mix on both counts, but the gap is small enough that it could be luck. Paper trading will show whether it holds up.

## Caveats

- Past results don't predict future ones. A rule set tuned until the backtest looks great usually does worse on new data. Tune on one period (e.g. 2015–2020) and check it on another (2021–2025).
- The default universe is sector ETFs, chosen to avoid *survivorship bias*: testing on stocks you know did well today inflates results.
- Taxes aren't modeled. Every gain here would be short-term.
