#!/usr/bin/env python3
"""
Layer 3: Walk-forward validation engine.

Public API
----------
    results, summary = run(strategy_name, ohlcv, n_folds, warmup, starting_balance)
    verdict          = report(strategy_name, results, summary)

    run()    — splits OHLCV into n_folds sequential out-of-sample windows,
               simulates each independently, returns raw metrics.
    report() — prints the per-fold table, aggregate row, pass/fail criteria,
               and a clear PASS / FAIL verdict.

Pass criteria (hard-coded, not configurable)
--------------------------------------------
ALL three must hold for a PASS:
    1. mean Sharpe > 0.5
    2. majority of folds profitable  (> 50%)
    3. median trades per fold >= 15

CLI
---
    python3 validate.py                         # all registered strategies, ETHUSDT 4h
    python3 validate.py aggressive              # one strategy
    python3 validate.py trend_follow --refresh  # force cache refresh
"""

import sys, os, math, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_data
import experiments
from strategies import REGISTRY, REQUIRES_FUNDING
from bot import compute_indicators, check_stop_tp, calc_position_eur, MIN_TRADE_EUR


# ── pass/fail thresholds ──────────────────────────────────────────────────────

PASS_CRITERIA = {
    'min_mean_sharpe':    0.5,   # annualised Sharpe on 4h daily-sampled returns
    'min_pct_profitable': 0.5,   # fraction of folds with positive return
    'min_median_trades':  15,    # trades per fold (statistical floor)
}


# ── simulation constants ──────────────────────────────────────────────────────

FEE_PCT      = 0.00075     # 0.075% maker (Binance BNB)
SLIPPAGE_PCT = 0.0002      # 0.02% limit-order slippage
WARMUP       = 200         # candles needed before indicators are valid
CANDLE_H     = 4           # hours per candle (4h data)
CPD          = 24 // CANDLE_H   # candles per day = 6


def _ts(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%d')


def _fold_stats(daily_rets, sharpe):
    """
    t-stat, two-tailed p-value, and Sharpe 95% CI for one fold's daily returns.
    No print side-effects — caller decides what to display.

    SE(Sharpe)  = sqrt((1 + 0.5 * sharpe^2) / n)   [Jobson-Korkie approximation]
    t-stat      = mean / (sample_std / sqrt(n))      [one-sample t-test, H0: mean=0]
    p-value     = erfc(|t| / sqrt(2))                [two-tailed, normal approximation]
    """
    n = len(daily_rets)
    if n < 2:
        return {'t_stat': 0.0, 'p_value': 1.0,
                'sharpe_ci_lo': sharpe, 'sharpe_ci_hi': sharpe}
    mu  = sum(daily_rets) / n
    sig = (sum((r - mu) ** 2 for r in daily_rets) / (n - 1)) ** 0.5  # sample std
    t   = mu / (sig / n ** 0.5) if sig > 0 else 0.0
    p   = math.erfc(abs(t) / math.sqrt(2))
    se  = ((1 + 0.5 * sharpe ** 2) / n) ** 0.5
    return {
        't_stat':        t,
        'p_value':       p,
        'sharpe_ci_lo':  sharpe - 1.96 * se,
        'sharpe_ci_hi':  sharpe + 1.96 * se,
    }


# ── fold simulation ───────────────────────────────────────────────────────────

def _simulate_fold(signal_fn, cfg, fold_ohlcv, warmup_len, starting_balance):
    """
    Simulate one fold.  fold_ohlcv[0:warmup_len] warms up indicators only
    (no trades placed during warmup).  Returns a metrics dict.
    """
    n = len(fold_ohlcv['closes'])

    state = {
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

    for i in range(warmup_len, n):
        window = {k: v[:i + 1] for k, v in fold_ohlcv.items()
                  if k not in ('times', 'funding')}
        ind    = compute_indicators(window)
        if 'funding' in fold_ohlcv:
            ind['funding_rate'] = fold_ohlcv['funding'][i]
        price  = ind['price']

        # Update trailing peak on open positions
        if state['coin_held'] > 1e-9 and state['trail_peak'] is not None:
            state['trail_peak'] = max(state['trail_peak'], price)

        # Hard exits take precedence over strategy signals
        forced = check_stop_tp(state, price, cfg)
        signal = forced[0] if forced else signal_fn(ind, state, i)

        ep = (price * (1 + SLIPPAGE_PCT) if signal == 'BUY'  else
              price * (1 - SLIPPAGE_PCT) if signal == 'SELL' else price)

        if signal == 'BUY' and state['balance'] >= MIN_TRADE_EUR and state['coin_held'] < 1e-9:
            eur_in = min(calc_position_eur(state, ep, ind, cfg), state['balance'])
            fee    = eur_in * FEE_PCT
            coins  = (eur_in - fee) / ep
            state['balance']      -= eur_in
            state['coin_held']    += coins
            state['entry_price']   = ep
            state['trail_peak']    = ep
            state['total_trades'] += 1
            state['buys']         += 1

        elif signal == 'SELL' and state['coin_held'] > 1e-9:
            entry  = state['entry_price'] or ep
            coins  = state['coin_held']
            gross  = coins * ep
            fee    = gross * FEE_PCT
            net    = gross - fee
            pnl    = net - coins * entry
            state['balance']         += net
            state['coin_held']        = 0.0
            state['entry_price']      = None
            state['trail_peak']       = None
            state['last_exit_check']  = i
            state['total_trades']    += 1
            state['sells']           += 1
            if pnl >= 0:
                state['win_trades']       += 1
                state['total_profit_eur'] += pnl
            else:
                state['loss_trades']      += 1
                state['total_loss_eur']   += abs(pnl)

        portfolio = state['balance'] + state['coin_held'] * price
        equity.append(portfolio)

        if (i - warmup_len) % CPD == 0:
            ret = (portfolio - prev_day_val) / prev_day_val if prev_day_val > 0 else 0.0
            daily_rets.append(ret)
            prev_day_val = portfolio

    # Mark open position to market
    final   = state['balance'] + state['coin_held'] * fold_ohlcv['closes'][-1]
    ret_pct = (final - starting_balance) / starting_balance * 100

    peak, max_dd = starting_balance, 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    if len(daily_rets) > 1:
        mu  = sum(daily_rets) / len(daily_rets)
        sig = (sum((r - mu) ** 2 for r in daily_rets) / len(daily_rets)) ** 0.5
        sharpe = (mu / sig) * math.sqrt(365) if sig > 0 else 0.0
    else:
        sharpe = 0.0

    closed   = state['win_trades'] + state['loss_trades']
    win_rate = state['win_trades'] / closed * 100 if closed > 0 else 0.0
    pf       = (state['total_profit_eur'] /
                max(state['total_loss_eur'], 1e-9))

    fs = _fold_stats(daily_rets, sharpe)
    return {
        'return_pct':    ret_pct,
        'sharpe':        sharpe,
        'max_dd':        max_dd * 100,
        'trades':        state['total_trades'],
        'win_rate':      win_rate,
        'pf':            min(pf, 9999.0),
        't_stat':        fs['t_stat'],
        'p_value':       fs['p_value'],
        'sharpe_ci_lo':  fs['sharpe_ci_lo'],
        'sharpe_ci_hi':  fs['sharpe_ci_hi'],
    }


# ── walk-forward engine ───────────────────────────────────────────────────────

def run(strategy_name, ohlcv, n_folds=12, warmup=WARMUP, starting_balance=1000.0):
    """
    Walk-forward validation.

    Parameters
    ----------
    strategy_name    : key in strategies.REGISTRY
    ohlcv            : dict from backtest_data.fetch()
    n_folds          : number of sequential test windows
    warmup           : indicator warm-up candles (excluded from trading)
    starting_balance : EUR per fold

    Returns
    -------
    results  — list of per-fold dicts; each has return_pct, sharpe, max_dd,
               trades, win_rate, pf, bh (buy-and-hold), t_start, t_end (ms)
    summary  — aggregate metrics dict
    """
    if strategy_name not in REGISTRY:
        raise ValueError(
            f'Unknown strategy "{strategy_name}". '
            f'Registered: {sorted(REGISTRY)}')

    signal_fn, cfg = REGISTRY[strategy_name]
    n      = len(ohlcv['closes'])
    usable = n - warmup
    fsz    = usable // n_folds

    results = []
    for k in range(n_folds):
        test_start = warmup + k * fsz
        test_end   = min(warmup + (k + 1) * fsz, n)
        ctx_start  = k * fsz

        fold_ohlcv  = {key: val[ctx_start:test_end] for key, val in ohlcv.items()}
        fold_result = _simulate_fold(signal_fn, cfg, fold_ohlcv, warmup, starting_balance)

        p0 = ohlcv['closes'][test_start]
        p1 = ohlcv['closes'][test_end - 1]
        fold_result['bh']      = (p1 - p0) / p0 * 100
        fold_result['t_start'] = ohlcv['times'][test_start]
        fold_result['t_end']   = ohlcv['times'][test_end - 1]
        results.append(fold_result)

    trades      = [r['trades'] for r in results]
    sharpes     = [r['sharpe'] for r in results]
    profitable  = sum(1 for r in results if r['return_pct'] > 0)
    sorted_tr   = sorted(trades)
    median_tr   = sorted_tr[n_folds // 2]

    summary = {
        'n_folds':         n_folds,
        'fold_candles':    fsz,
        'fold_days':       fsz * CANDLE_H / 24,
        'mean_sharpe':     sum(sharpes) / n_folds,
        'pos_sharpe':      sum(1 for s in sharpes if s > 0),
        'profitable':      profitable,
        'pct_profitable':  profitable / n_folds,
        'total_trades':    sum(trades),
        'avg_trades':      sum(trades) / n_folds,
        'median_trades':   median_tr,
    }
    return results, summary


# ── reporting ─────────────────────────────────────────────────────────────────

def report(strategy_name, results, summary, data_meta=None):
    """
    Print per-fold table, aggregate row, pass/fail criteria, and PASS/FAIL verdict.
    Returns the verdict string ('PASS' or 'FAIL').

    If data_meta is provided ({symbol, interval, start, end, candles}), the run
    is automatically appended to experiments.jsonl via experiments.log().
    """
    cfg  = REGISTRY[strategy_name][1]
    desc = cfg.get('desc', strategy_name)

    n_folds = summary['n_folds']
    fsz     = summary['fold_candles']
    days    = summary['fold_days']
    W       = 108

    print(f'\n{"─" * W}')
    print(f'  {strategy_name.upper()}')
    print(f'  {desc}')
    print(f'  {n_folds} folds × {fsz} candles ({days:.0f} days ≈ {days / 30:.1f} months per fold)')
    print(f'{"─" * W}')
    print(f'{"Fold":<5} {"Period":<25} {"Return":>8} {"Sharpe":>7} '
          f'{"WinRate":>8} {"Trades":>7} {"MaxDD":>7} {"PF":>7} {"B&H":>8}'
          f' {"t-stat":>7} {"p":>7}')
    print('─' * W)

    for k, r in enumerate(results):
        # ★ = clears all three pass criteria at the individual-fold level
        starred = r['sharpe'] > 0 and r['pf'] > 1.0 and r['trades'] >= 5
        flag    = ' ★' if starred else ''
        period  = f'{_ts(r["t_start"])} → {_ts(r["t_end"])}'
        print(f'{k + 1:<5} {period:<25} {r["return_pct"]:>+7.2f}% {r["sharpe"]:>7.2f} '
              f'{r["win_rate"]:>7.0f}% {r["trades"]:>7} {r["max_dd"]:>6.1f}% '
              f'{r["pf"]:>7.2f} {r["bh"]:>+7.2f}%'
              f' {r["t_stat"]:>+7.2f} {r["p_value"]:>7.3f}{flag}')

    ms  = summary['mean_sharpe']
    psp = summary['pos_sharpe']
    prf = summary['profitable']
    tot = summary['total_trades']
    med = summary['median_trades']
    avg = summary['avg_trades']
    print('─' * W)
    print(f'  AGGREGATE  mean Sharpe {ms:+.2f} | pos Sharpe {psp}/{n_folds} | '
          f'profitable {prf}/{n_folds} | '
          f'median {med} / avg {avg:.1f} trades/fold  (total {tot})')

    # Evaluate each pass criterion
    c = PASS_CRITERIA
    checks = {
        'mean_sharpe':    (summary['mean_sharpe']   >  c['min_mean_sharpe'],
                           f'mean Sharpe > {c["min_mean_sharpe"]}',
                           f'{ms:+.2f}'),
        'pct_profitable': (summary['pct_profitable'] >  c['min_pct_profitable'],
                           f'majority folds profitable (>{c["min_pct_profitable"]*100:.0f}%)',
                           f'{prf}/{n_folds}'),
        'median_trades':  (summary['median_trades']  >= c['min_median_trades'],
                           f'median trades/fold ≥ {c["min_median_trades"]}',
                           f'{med}'),
    }
    verdict = 'PASS' if all(v[0] for v in checks.values()) else 'FAIL'

    print()
    print(f'  PASS CRITERIA')
    for ok, label, actual in checks.values():
        mark = '✓' if ok else '✗'
        print(f'    {mark}  {label:<45}  actual: {actual}')
    print(f'  {"─" * 55}')
    print(f'  VERDICT: {verdict}')
    print(f'{"─" * W}')

    if data_meta is not None:
        experiments.log(strategy_name, REGISTRY[strategy_name][1], data_meta, summary, verdict)

    return verdict


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    argv    = sys.argv[1:]
    refresh = '--refresh' in argv
    args    = [a for a in argv if not a.startswith('--')]

    names = [a for a in args if a in REGISTRY]
    if not names:
        names = list(REGISTRY)

    # Data config
    symbol   = 'ETHUSDT'
    interval = '4h'
    start    = '2020-01-01'

    W = 108
    print(f'{"=" * W}')
    print(f'  Walk-Forward Validation | {symbol} {interval} | WARMUP={WARMUP}')
    print(f'  PASS: mean Sharpe>{PASS_CRITERIA["min_mean_sharpe"]}  AND  '
          f'>50% folds profitable  AND  '
          f'median trades≥{PASS_CRITERIA["min_median_trades"]}')
    print(f'{"=" * W}')

    ohlcv = backtest_data.fetch(symbol, interval, start_date=start, force_refresh=refresh)
    n = len(ohlcv['closes'])
    print(f'OHLCV    {n} candles  '
          f'{backtest_data._date(ohlcv["times"][0])} → '
          f'{backtest_data._date(ohlcv["times"][-1])}')

    # Fetch and align funding data if any queued strategy requires it.
    # This is done once, regardless of how many funding strategies are in the run.
    funding     = None
    ohlcv_fund  = ohlcv     # same object unless funding is available
    if any(name in REQUIRES_FUNDING for name in names):
        funding    = backtest_data.fetch_funding(symbol, force_refresh=refresh)
        ohlcv_fund = {**ohlcv, 'funding': backtest_data.align_funding(ohlcv['times'], funding)}
        print(f'Funding  {len(funding["times"])} events  '
              f'{backtest_data._date(funding["times"][0])} → '
              f'{backtest_data._date(funding["times"][-1])}')

    data_meta = {
        'symbol':   symbol,
        'interval': interval,
        'start':    backtest_data._date(ohlcv['times'][0]),
        'end':      backtest_data._date(ohlcv['times'][-1]),
        'candles':  n,
        'warmup':   WARMUP,
    }

    all_results   = {}
    all_summaries = {}
    for name in names:
        ohlcv_use = ohlcv_fund if name in REQUIRES_FUNDING else ohlcv
        all_results[name], all_summaries[name] = run(name, ohlcv_use)

    verdicts = {}
    for name in names:
        dm = dict(data_meta)
        if name in REQUIRES_FUNDING and funding is not None:
            dm['funding_events'] = len(funding['times'])
            dm['funding_start']  = backtest_data._date(funding['times'][0])
            dm['funding_end']    = backtest_data._date(funding['times'][-1])
        verdicts[name] = report(name, all_results[name], all_summaries[name], dm)

    print(f'\n{"=" * W}')
    print(f'  ★  = Sharpe>0  AND  PF>1.0  AND  ≥5 trades  (individual-fold bar)')
    print()
    for name, v in verdicts.items():
        ms  = all_summaries[name]['mean_sharpe']
        prf = all_summaries[name]['profitable']
        nf  = all_summaries[name]['n_folds']
        med = all_summaries[name]['median_trades']
        print(f'  {name.upper():<16}  {v}  '
              f'(mean Sharpe {ms:+.2f}, {prf}/{nf} profitable, median {med} trades/fold)')
    print(f'{"=" * W}')


if __name__ == '__main__':
    main()
