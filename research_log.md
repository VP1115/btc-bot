# Research Log

---

## Experiment 001 — Aggressive Baseline Documentation
**Date:** 2026-06-21
**Status:** BASELINE (pre-validation)
**Strategy:** aggressive
**Type:** Mean reversion

### Signal Logic
- **BUY:** RSI < 32 AND (MACD bullish OR price ≤ lower BB) AND ADX ≥ 20 AND price > SMA200
- **SELL:** RSI > 68 AND (MACD bearish OR price ≥ upper BB)
- **Hard exits:** Stop-loss −5%, take-profit +10%, trailing stop arms at +3% gain, trails 2% below peak
- **Position sizing:** ATR-sized, 2% portfolio risk per trade, max 60% cash

### Data
- Source: Binance OHLCV via backtest_data.py cache
- Pairs tested: BTCUSDT, ETHUSDT, SOLUSDT — 1h timeframe
- Walk-forward: validate.py, 12 folds, ETHUSDT 4h — **NOT YET RUN**

### Results (live paper trading, as of 2026-06-21)
| Asset | Trades | Closed P&L | Notes |
|-------|--------|------------|-------|
| BTC | 7 (5B/2S) | Unprofitable | Best PF 0.92 across ADX/RSI grid. Disabled May 2026. Open position unrealized −€57. |
| ETH | 14 (5B/9S) | −€54.40 (−2.72%) | 2 wins (+€36.01 total), 1 stop-loss (−€97.11). Currently in cash. |
| SOL | 7 (5B/2S) | +€10.09 closed | Sharpe 0.30 in backtest, below 0.5 threshold. Disabled May 2026. Open position unrealized −€26. |

### Walk-Forward Verdict
**NOT RUN** — `experiments.jsonl` does not exist. No formal verdict yet.

### Observations
1. Fee drag (0.17% round trip) requires ~2.5% cumulative return per 15-trade fold just to break even
2. BTC mean reversion unprofitable across full ADX/RSI threshold grid
3. ETH single stop-loss (−€97) wiped out 2.7× the combined winning trades (+€36)
4. SOL Sharpe 0.30 likely reflects fee drag compounded by premature trailing stop exits

### Next Steps → Experiment 002
- [ ] Run `python3 validate.py aggressive` to produce first `experiments.jsonl` entry
- [ ] Add Sharpe confidence interval and t-statistic to `backtest.py`
- [ ] Test RSI thresholds 28/72 and 30/70 on ETH
- [ ] Compare `aggressive` vs `trend_follow` on ETHUSDT 4h

---
