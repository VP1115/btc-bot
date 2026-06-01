# Backtesting & Walk-Forward Validation Framework

`Python` | `data pipeline` | `simulation engine` | `walk-forward validation` | `experiment tracking`

A structured research framework demonstrating systematic data-pipeline construction, out-of-sample validation methodology, and reproducible experiment tracking — applied to cryptocurrency market data. The engineering goal is honest evaluation: strategies that do not generalize across unseen sequential data windows are explicitly rejected with a FAIL verdict.

---

## What it does

This framework fetches and caches years of OHLCV data from the Binance API, simulates configurable strategies with realistic exchange costs, then validates them using walk-forward testing across 12 sequential non-overlapping market windows. Every validation run is logged to an append-only experiment file for full reproducibility. A live paper-trading engine and terminal dashboard are also included for running strategies in real time.

---

## Key features

- **Paginated data pipeline with local cache** — fetch years of 1h or 4h candles from Binance in one command; subsequent runs load from disk automatically; `--refresh` forces a new API pull
- **Realistic simulation** — maker/taker fee modes (0.075% or 0.1% per side), limit-order slippage (0.02%), configurable no-fee sanity-check mode
- **Walk-forward validation engine** — splits the full dataset into 12 sequential out-of-sample folds; simulates each independently; applies three hard PASS criteria:
  - Mean annualised Sharpe > 0.5
  - Majority of folds profitable (> 50%)
  - Median trades per fold >= 15 (statistical floor to prevent lucky-sparse results)
- **Append-only experiment log** — every validation run writes one structured JSON record (strategy config, data provenance, results, verdict) to `experiments.jsonl`; nothing is ever overwritten
- **Pluggable strategy registry** — new strategies implement a single `signal_fn(ind, state, check_num) -> BUY | SELL | HOLD` contract and register in one place (`strategies.py`)
- **Portfolio risk controls** — `risk_manager.py` creates a PAUSE file to halt all bots if daily portfolio loss exceeds 5% or drawdown exceeds 15%
- **Terminal dashboard** — `curses`-based live monitoring of multi-asset paper positions; zero external dependencies

---

## Tech stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3 (standard library only — no pip install needed for the research pipeline) |
| Data sources | Binance REST API (`/api/v3/klines`), CryptoCompare API |
| Storage | Local JSON cache (`.cache/`), append-only JSONL experiment log |

---

## Architecture

The project is structured as four sequential, independently runnable layers:

```
backtest_data.py    Layer 1: Data pipeline
                    Fetches and paginates Binance OHLCV.
                    Caches to .cache/{SYMBOL}_{interval}.json with full provenance metadata.

backtest.py         Layer 2: Simulation engine
                    Replays candle-by-candle with full state tracking: position sizing
                    (risk-% of portfolio), entry/exit, stop-loss/take-profit/trailing stop,
                    slippage-adjusted P&L. Computes Sharpe, max drawdown, win rate, profit
                    factor. Renders ASCII equity curve.

validate.py         Layer 3: Walk-forward validation
                    Splits OHLCV into n_folds sequential windows; simulates each independently
                    with a 200-candle indicator warmup period. Applies hard PASS/FAIL criteria;
                    prints per-fold table and aggregate verdict. Auto-logs to experiments.jsonl.

experiments.py      Layer 4: Experiment tracker
                    Append-only log of every validation run. Stores strategy config, data
                    metadata (symbol, interval, date range, candle count), validation summary,
                    and PASS/FAIL verdict. CLI prints the log newest-first.
```

Supporting modules: `strategies.py` (strategy registry), `bot.py` (live signal and indicator engine), `risk_manager.py` (drawdown controls), `dashboard.py` (terminal monitor).

---

## Setup and usage

No external packages are required for the research pipeline.

```bash
git clone https://github.com/VP1115/btc-bot
cd btc-bot
```

**Walk-forward validation (main entry point):**
```bash
python3 validate.py                          # all strategies on ETHUSDT 4h
python3 validate.py aggressive               # one strategy
python3 validate.py aggressive --refresh     # force fresh Binance data pull
```

**Single backtest:**
```bash
python3 backtest.py aggressive BTC 1000                  # 90d, 1h, default fees
python3 backtest.py aggressive BTC 1000 --timeframe 4h
python3 backtest.py aggressive BTC 1000 --no-fees        # sanity check
python3 backtest.py aggressive BTC 1000 --binance-taker  # worst-case fees
```

**Experiment history:**
```bash
python3 experiments.py        # full log, newest first
python3 experiments.py 10     # last 10 runs
```

**Live paper trading** requires Binance API keys (copy `.env.example` to `.env`).

---

## Status

Personal project. Research and paper trading only. Not financial advice.
