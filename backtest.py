#!/usr/bin/env python3
"""
Backtester — simulate a strategy on 90 days of historical hourly data.
Usage: python3 backtest.py [strategy] [name] [starting_balance]
       python3 backtest.py aggressive BTC 2000
       python3 backtest.py aggressive SOL 1000
Fees: 0.1% per trade + 0.05% slippage.
"""

import sys, os, math, logging
logging.basicConfig(level=logging.WARNING)   # suppress INFO from bot.py imports

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import (
    _get_json, _fetch_cryptocompare, compute_indicators,
    get_signal, check_stop_tp, calc_position_eur,
    STRATEGIES, MIN_TRADE_EUR,
)

FEE_PCT      = 0.001    # 0.1%
SLIPPAGE_PCT = 0.0005   # 0.05%

# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_history(symbol, days=90):
    """Fetch up to 90 days of hourly OHLCV from CryptoCompare (free, no key)."""
    limit = min(days * 24, 2000)
    print(f'Fetching {limit}h of history for {symbol} from CryptoCompare…')
    return _fetch_cryptocompare(symbol, limit)

# ── Simulation ─────────────────────────────────────────────────────────────────

def run_backtest(strategy, symbol, starting_balance=1000.0, days=90):
    cfg = STRATEGIES[strategy]

    ohlcv = fetch_history(symbol, days)
    n     = len(ohlcv['closes'])
    print(f'Got {n} candles  ({ohlcv["closes"][0]:.2f} → {ohlcv["closes"][-1]:.2f})\n')

    state = {
        'strategy':         strategy,
        'balance':          starting_balance,
        'coin_held':        0.0,
        'entry_price':      None,
        'trail_peak':       None,
        'total_trades':     0,
        'buys':             0,
        'sells':            0,
        'win_trades':       0,
        'loss_trades':      0,
        'total_profit_eur': 0.0,
        'total_loss_eur':   0.0,
    }

    equity       = [starting_balance]
    daily_rets   = []
    prev_day_val = starting_balance
    warmup       = 200  # need ~200 candles for ADX + MACD to be ready

    for i in range(warmup, n):
        window = {k: v[:i+1] for k, v in ohlcv.items()}
        ind    = compute_indicators(window)
        price  = ind['price']

        # Update trailing peak
        if state['coin_held'] > 1e-9 and state.get('trail_peak') is not None:
            state['trail_peak'] = max(state['trail_peak'], price)

        forced = check_stop_tp(state, price, cfg)
        if forced:
            signal, reason = forced[0], forced[1]
        else:
            signal, reasons, _ = get_signal(ind, strategy)
            reason = reasons[0] if reasons else ''

        # Apply slippage to execution price
        if signal == 'BUY':
            exec_price = price * (1 + SLIPPAGE_PCT)
        elif signal == 'SELL':
            exec_price = price * (1 - SLIPPAGE_PCT)
        else:
            exec_price = price

        if signal == 'BUY' and state['balance'] >= MIN_TRADE_EUR and state['coin_held'] < 1e-9:
            eur_in   = min(calc_position_eur(state, exec_price, ind, cfg), state['balance'])
            coin_out = eur_in / exec_price
            fee      = eur_in * FEE_PCT
            state['balance']    -= eur_in + fee
            state['coin_held']  += coin_out
            state['entry_price'] = exec_price
            state['trail_peak']  = exec_price
            state['total_trades'] += 1
            state['buys']         += 1

        elif signal == 'SELL' and state['coin_held'] > 1e-9:
            entry    = state.get('entry_price') or exec_price
            coin_out = state['coin_held']
            eur_in   = coin_out * exec_price
            fee      = eur_in * FEE_PCT
            pnl      = eur_in - fee - coin_out * entry
            state['balance']    += eur_in - fee
            state['coin_held']   = 0.0
            state['entry_price'] = None
            state['trail_peak']  = None
            state['total_trades'] += 1
            state['sells']        += 1
            if pnl >= 0:
                state['win_trades']       += 1
                state['total_profit_eur'] += pnl
            else:
                state['loss_trades']      += 1
                state['total_loss_eur']   += abs(pnl)

        portfolio = state['balance'] + state['coin_held'] * price
        equity.append(portfolio)

        # Daily return (every 24 candles)
        if (i - warmup) % 24 == 0:
            ret = (portfolio - prev_day_val) / prev_day_val if prev_day_val > 0 else 0.0
            daily_rets.append(ret)
            prev_day_val = portfolio

    final_val = equity[-1]
    total_ret = (final_val - starting_balance) / starting_balance * 100

    # Max drawdown
    peak, max_dd = starting_balance, 0.0
    for v in equity:
        if v > peak: peak = v
        max_dd = max(max_dd, (peak - v) / peak)

    # Sharpe ratio (annualised, risk-free = 0)
    if len(daily_rets) > 1:
        mu    = sum(daily_rets) / len(daily_rets)
        sigma = (sum((r - mu) ** 2 for r in daily_rets) / len(daily_rets)) ** 0.5
        sharpe = (mu / sigma) * math.sqrt(365) if sigma > 0 else 0.0
    else:
        sharpe = 0.0

    closed     = state['win_trades'] + state['loss_trades']
    win_rate   = state['win_trades'] / closed * 100 if closed > 0 else 0.0
    pf         = state['total_profit_eur'] / max(state['total_loss_eur'], 1e-9)
    buy_hold   = ohlcv['closes'][-1] / ohlcv['closes'][warmup] * 100 - 100

    _print_results(strategy, symbol, starting_balance, final_val, total_ret,
                   sharpe, max_dd, state, win_rate, pf, buy_hold)
    _ascii_chart(equity, width=60, height=14)
    return {
        'total_return_pct':  total_ret,
        'final_value':       final_val,
        'sharpe':            sharpe,
        'max_drawdown_pct':  max_dd * 100,
        'total_trades':      state['total_trades'],
        'win_rate':          win_rate,
        'profit_factor':     pf,
        'buy_hold_pct':      buy_hold,
    }

# ── Output ─────────────────────────────────────────────────────────────────────

def _print_results(strategy, symbol, start, final, ret, sharpe, max_dd,
                   state, win_rate, pf, buy_hold):
    print(f'\n{"="*54}')
    print(f'  Backtest: {symbol}  strategy={strategy}  90 days')
    print(f'{"="*54}')
    print(f'  Total Return    : {ret:>+7.2f}%  (buy & hold: {buy_hold:>+.2f}%)')
    print(f'  Start → End     :  €{start:>9,.2f} → €{final:>9,.2f}')
    print(f'  Sharpe Ratio    : {sharpe:>7.2f}')
    print(f'  Max Drawdown    : {max_dd*100:>7.1f}%')
    print(f'  Total Trades    : {state["total_trades"]:>4}  ({state["buys"]} buys / {state["sells"]} sells)')
    print(f'  Win Rate        : {win_rate:>7.0f}%  ({state["win_trades"]}W / {state["loss_trades"]}L)')
    print(f'  Profit Factor   : {pf:>7.2f}')
    print(f'{"="*54}')

def _ascii_chart(equity, width=60, height=14):
    if not equity:
        return
    step   = max(1, len(equity) // width)
    pts    = equity[::step][:width]
    lo, hi = min(pts), max(pts)
    rng    = hi - lo or 1.0

    print('\n  Equity curve (90 days):')
    for row in range(height, -1, -1):
        threshold = lo + rng * row / height
        bar       = ''.join('█' if v >= threshold else ' ' for v in pts)
        if row == height:
            label = f' €{hi:,.0f}'
        elif row == height // 2:
            label = f' €{(lo+hi)/2:,.0f}'
        elif row == 0:
            label = f' €{lo:,.0f}'
        else:
            label = ''
        print(f'  │{bar}{label}')
    print(f'  └{"─"*width}')
    print(f'   90d ago{" "*(width-13)}now')

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    strategy        = sys.argv[1].lower() if len(sys.argv) > 1 else 'aggressive'
    name            = sys.argv[2].upper() if len(sys.argv) > 2 else 'BTC'
    starting_balance = float(sys.argv[3]) if len(sys.argv) > 3 else 2000.0

    if strategy not in STRATEGIES:
        print(f'Strategy must be one of: {", ".join(STRATEGIES)}')
        sys.exit(1)

    symbol_map = {'BTC': 'BTCEUR', 'ETH': 'ETHEUR', 'SOL': 'SOLEUR'}
    symbol = symbol_map.get(name, f'{name}EUR')

    run_backtest(strategy, symbol, starting_balance)

if __name__ == '__main__':
    main()
