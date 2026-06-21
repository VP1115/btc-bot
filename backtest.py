#!/usr/bin/env python3
"""
Backtester — simulate a strategy on 90 days of historical data.
Usage: python3 backtest.py [strategy] [name] [starting_balance] [flags]
       python3 backtest.py aggressive BTC 1000
       python3 backtest.py aggressive BTC 1000 --timeframe 4h
       python3 backtest.py aggressive BTC 1000 --no-fees
       python3 backtest.py aggressive BTC 1000 --binance-taker

Fees (default): 0.075%/side maker + 0.02% slippage  (Binance BNB discount)
--binance-taker: 0.1%/side taker + 0.02% slippage   (worst case)
--no-fees:       0% fees, 0% slippage                (sanity check)
--timeframe:     1h (default) or 4h
"""

import sys, os, math, logging
logging.basicConfig(level=logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import (
    _get_json, compute_indicators,
    get_signal, check_stop_tp, calc_position_eur,
    STRATEGIES, MIN_TRADE_EUR,
)

# Default: Binance maker fee with BNB discount
DEFAULT_FEE      = 0.00075   # 0.075% per side
DEFAULT_SLIPPAGE = 0.0002    # 0.02% (limit orders)
TAKER_FEE        = 0.001     # 0.1% per side


# ── Statistical significance ───────────────────────────────────────────────────

def calc_stats(daily_rets, sharpe):
    """
    Compute statistical significance metrics for a backtest run.

    Parameters
    ----------
    daily_rets : list of float — one return observation per trading day
    sharpe     : float — annualised Sharpe ratio already computed by the caller

    Returns
    -------
    dict with keys: n, mean, std, t_stat, p_value, sharpe_ci_lo, sharpe_ci_hi

    Formulas
    --------
    SE(Sharpe)  = sqrt((1 + 0.5 * sharpe^2) / n)   [Jobson-Korkie approximation]
    CI          = sharpe ± 1.96 * SE                 [95% two-sided]
    t-stat      = mean / (std / sqrt(n))             [one-sample t-test, H0: mean=0]
    p-value     = erfc(|t| / sqrt(2))                [two-tailed, normal approximation]
    """
    n = len(daily_rets)
    if n < 30:
        print(f'  ⚠  WARNING: only {n} daily observations — '
              f'sample too small for reliable inference (need ≥ 30)')
    if n < 2:
        return {
            'n': n, 'mean': 0.0, 'std': 0.0,
            't_stat': 0.0, 'p_value': 1.0,
            'sharpe_ci_lo': sharpe, 'sharpe_ci_hi': sharpe,
        }

    mean = sum(daily_rets) / n
    std  = (sum((r - mean) ** 2 for r in daily_rets) / (n - 1)) ** 0.5  # sample std
    t    = mean / (std / n ** 0.5) if std > 0 else 0.0
    p    = math.erfc(abs(t) / math.sqrt(2))

    se    = ((1 + 0.5 * sharpe ** 2) / n) ** 0.5
    ci_lo = sharpe - 1.96 * se
    ci_hi = sharpe + 1.96 * se

    return {
        'n':             n,
        'mean':          mean,
        'std':           std,
        't_stat':        t,
        'p_value':       p,
        'sharpe_ci_lo':  ci_lo,
        'sharpe_ci_hi':  ci_hi,
    }


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_history(symbol, timeframe='1h', days=90):
    """Fetch OHLCV from CryptoCompare. Timeframe: '1h' or '4h'."""
    for suffix in ('EUR', 'USDT', 'USD'):
        if symbol.endswith(suffix):
            fsym = symbol[:-len(suffix)]
            tsym = suffix if suffix in ('EUR', 'USD') else 'USD'
            break
    else:
        raise ValueError(f'Cannot parse symbol: {symbol}')

    if timeframe == '4h':
        limit     = min(days * 6, 2000)
        aggregate = 4
        candle_h  = 4
    else:  # 1h
        limit     = min(days * 24, 2000)
        aggregate = 1
        candle_h  = 1

    url  = (f'https://min-api.cryptocompare.com/data/v2/histohour'
            f'?fsym={fsym}&tsym={tsym}&limit={limit}&aggregate={aggregate}')
    data = _get_json(url)
    if data.get('Response') != 'Success':
        raise RuntimeError(f'CryptoCompare: {data.get("Message")}')
    c = data['Data']['Data']
    ohlcv = {
        'opens':   [float(x['open'])       for x in c],
        'highs':   [float(x['high'])       for x in c],
        'lows':    [float(x['low'])        for x in c],
        'closes':  [float(x['close'])      for x in c],
        'volumes': [float(x['volumefrom']) for x in c],
    }
    print(f'Fetched {len(c)} {timeframe} candles for {symbol}  '
          f'({ohlcv["closes"][0]:.2f} → {ohlcv["closes"][-1]:.2f})')
    return ohlcv, candle_h

# ── Simulation ─────────────────────────────────────────────────────────────────

def run_backtest(strategy, symbol, starting_balance=1000.0, days=90,
                 no_fees=False, taker=False, timeframe='1h'):
    cfg = STRATEGIES[strategy]

    if no_fees:
        fee_pct, slippage_pct = 0.0, 0.0
        fee_label = 'no fees/slippage'
    elif taker:
        fee_pct, slippage_pct = TAKER_FEE, DEFAULT_SLIPPAGE
        fee_label = f'taker {TAKER_FEE*100:.3f}%/side + slippage {DEFAULT_SLIPPAGE*100:.2f}%'
    else:
        fee_pct, slippage_pct = DEFAULT_FEE, DEFAULT_SLIPPAGE
        fee_label = f'maker {DEFAULT_FEE*100:.3f}%/side + slippage {DEFAULT_SLIPPAGE*100:.2f}%'

    ohlcv, candle_h = fetch_history(symbol, timeframe, days)
    n = len(ohlcv['closes'])
    print(f'[{fee_label}]\n')

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
        'last_exit_check':  None,
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
            # funding_rate=None: historical funding data is unavailable in backtests.
            # The funding rate filter is inactive here and only operates live.
            signal, reasons, _ = get_signal(ind, strategy, state, i, None)
            reason = reasons[0] if reasons else ''

        # Apply slippage to execution price
        if signal == 'BUY':
            exec_price = price * (1 + slippage_pct)
        elif signal == 'SELL':
            exec_price = price * (1 - slippage_pct)
        else:
            exec_price = price

        if signal == 'BUY' and state['balance'] >= MIN_TRADE_EUR and state['coin_held'] < 1e-9:
            eur_in   = min(calc_position_eur(state, exec_price, ind, cfg), state['balance'])
            coin_out = eur_in / exec_price
            fee      = eur_in * fee_pct
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
            fee      = eur_in * fee_pct
            pnl      = eur_in - fee - coin_out * entry
            state['balance']         += eur_in - fee
            state['coin_held']        = 0.0
            state['entry_price']      = None
            state['trail_peak']       = None
            state['last_exit_check']  = i  # mirrors execute_trade(); enables cooldown in backtest
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

        # Daily return (every 24h worth of candles)
        candles_per_day = max(1, 24 // candle_h)
        if (i - warmup) % candles_per_day == 0:
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
                   sharpe, max_dd, state, win_rate, pf, buy_hold, fee_label, timeframe)
    _print_stats(calc_stats(daily_rets, sharpe))
    _ascii_chart(equity, width=60, height=14, timeframe=timeframe)
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
                   state, win_rate, pf, buy_hold, fee_label='', timeframe='1h'):
    print(f'\n{"="*54}')
    print(f'  Backtest: {symbol}  strategy={strategy}  90d  {timeframe}')
    print(f'  {fee_label}')
    print(f'{"="*54}')
    print(f'  Total Return    : {ret:>+7.2f}%  (buy & hold: {buy_hold:>+.2f}%)')
    print(f'  Start → End     :  €{start:>9,.2f} → €{final:>9,.2f}')
    print(f'  Sharpe Ratio    : {sharpe:>7.2f}')
    print(f'  Max Drawdown    : {max_dd*100:>7.1f}%')
    print(f'  Total Trades    : {state["total_trades"]:>4}  ({state["buys"]} buys / {state["sells"]} sells)')
    print(f'  Win Rate        : {win_rate:>7.0f}%  ({state["win_trades"]}W / {state["loss_trades"]}L)')
    print(f'  Profit Factor   : {pf:>7.2f}')
    print(f'{"="*54}')

def _print_stats(stats):
    n     = stats['n']
    mean  = stats['mean']
    std   = stats['std']
    t     = stats['t_stat']
    p     = stats['p_value']
    ci_lo = stats['sharpe_ci_lo']
    ci_hi = stats['sharpe_ci_hi']

    if p < 0.01:
        sig_note  = 'significant at 0.01'
        inference = 'STRONG — p < 0.01'
    elif p < 0.05:
        sig_note  = 'significant at 0.05'
        inference = 'SIGNIFICANT — p < 0.05'
    elif p < 0.10:
        sig_note  = 'not significant at 0.05'
        inference = 'WEAK — p < 0.10, treat with caution'
    else:
        sig_note  = 'not significant at 0.05'
        inference = 'NOT SIGNIFICANT — p > 0.10, results likely noise'

    W = 54
    print(f'\n{"=" * W}')
    print(f'  Statistical Significance')
    print(f'{"=" * W}')
    print(f'  Observations (daily)  : {n:>5}')
    print(f'  Mean daily return     : {mean * 100:>+7.3f}%')
    print(f'  Std daily return      : {std  * 100:>7.3f}%')
    print(f'  t-statistic           : {t:>6.2f}')
    print(f'  p-value (two-tailed)  : {p:>7.3f}   ({sig_note})')
    print(f'  Sharpe 95% CI         :  [{ci_lo:.2f}, {ci_hi:.2f}]')
    print(f'  Inference             :  {inference}')
    print(f'{"=" * W}')


def _ascii_chart(equity, width=60, height=14, timeframe='1h'):
    if not equity:
        return
    step   = max(1, len(equity) // width)
    pts    = equity[::step][:width]
    lo, hi = min(pts), max(pts)
    rng    = hi - lo or 1.0

    print(f'\n  Equity curve (90 days, {timeframe} candles):')
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
    argv = sys.argv[1:]
    no_fees   = '--no-fees'       in argv
    taker     = '--binance-taker' in argv
    tf_flag   = next((a.split('=')[1] if '=' in a else argv[argv.index(a)+1]
                      for a in argv if a.startswith('--timeframe')), '1h')
    timeframe = tf_flag if tf_flag in ('1h', '4h') else '1h'

    args             = [a for a in argv if not a.startswith('--') and a not in ('1h', '4h')]
    strategy         = args[0].lower() if len(args) > 0 else 'aggressive'
    name             = args[1].upper() if len(args) > 1 else 'BTC'
    starting_balance = float(args[2]) if len(args) > 2 else 1000.0

    if strategy not in STRATEGIES:
        print(f'Strategy must be one of: {", ".join(STRATEGIES)}')
        sys.exit(1)

    symbol_map = {'BTC': 'BTCEUR', 'ETH': 'ETHEUR', 'SOL': 'SOLEUR'}
    symbol = symbol_map.get(name, f'{name}EUR')

    run_backtest(strategy, symbol, starting_balance,
                 no_fees=no_fees, taker=taker, timeframe=timeframe)

if __name__ == '__main__':
    main()
