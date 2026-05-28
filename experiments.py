#!/usr/bin/env python3
"""
Layer 4: Experiment tracking.

append-only log in experiments.jsonl (one JSON object per line).
Every call to log() adds exactly one record; nothing is ever modified.

Record schema
─────────────
{
  "ts":         "2026-05-28T13:30:00Z",     # UTC timestamp of the run
  "strategy":   "aggressive",
  "config":     {stop_loss_pct, take_profit_pct, trail_trigger, trail_pct,
                 risk_pct, max_pos_pct, adx_min,
                 rsi_oversold, rsi_overbought, …},   # all non-display keys
  "data":       {"symbol": "ETHUSDT", "interval": "4h",
                 "start": "2020-01-01", "end": "2026-05-28", "candles": 14037},
  "validation": {"n_folds": 12, "warmup": 200, "fold_days": 192.0},
  "results":    {"mean_sharpe": -0.38, "pct_profitable": 0.50,
                 "median_trades": 4, "total_trades": 50, "avg_trades": 4.2},
  "verdict":    "FAIL"
}

Public API
──────────
    log(strategy_name, config, data_meta, summary, verdict)
    show(n=None)    # print table, newest first

CLI
───
    python3 experiments.py          # print full history
    python3 experiments.py 10       # last 10 records
"""

import sys, os, json, datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'experiments.jsonl')

# Keys stripped from config before logging (display-only; not strategy parameters)
_SKIP_CONFIG = {'desc'}


# ── write ─────────────────────────────────────────────────────────────────────

def log(strategy_name, config, data_meta, summary, verdict):
    """
    Append one experiment record to experiments.jsonl.

    Parameters
    ----------
    strategy_name : str   — key from strategies.REGISTRY
    config        : dict  — strategy config dict (bot.STRATEGIES[name])
    data_meta     : dict  — {symbol, interval, start, end, candles}
    summary       : dict  — output of validate.run()[1]
    verdict       : str   — 'PASS' or 'FAIL'
    """
    record = {
        'ts':       datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'strategy': strategy_name,
        'config':   {k: v for k, v in config.items() if k not in _SKIP_CONFIG},
        'data': {
            'symbol':   data_meta['symbol'],
            'interval': data_meta['interval'],
            'start':    data_meta['start'],
            'end':      data_meta['end'],
            'candles':  data_meta['candles'],
        },
        'validation': {
            'n_folds':   summary['n_folds'],
            'warmup':    data_meta.get('warmup', 200),
            'fold_days': round(summary['fold_days'], 1),
        },
        'results': {
            'mean_sharpe':    round(summary['mean_sharpe'],   3),
            'pct_profitable': round(summary['pct_profitable'], 3),
            'median_trades':  summary['median_trades'],
            'total_trades':   summary['total_trades'],
            'avg_trades':     round(summary['avg_trades'],    1),
        },
        'verdict': verdict,
    }
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, separators=(',', ':')) + '\n')


# ── read ──────────────────────────────────────────────────────────────────────

def _load(n=None):
    """Return list of records (oldest first). Skips malformed lines."""
    if not os.path.exists(LOG_FILE):
        return []
    records = []
    with open(LOG_FILE, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f'experiments.jsonl: skipping malformed line {lineno}', file=sys.stderr)
    if n is not None:
        records = records[-n:]
    return records


# ── display ───────────────────────────────────────────────────────────────────

def show(n=None):
    """
    Print the experiment log as a fixed-width table, newest first.

    Parameters
    ----------
    n : int or None — if given, show only the most recent n records
    """
    records = _load(n)
    if not records:
        print(f'No experiments logged yet  ({LOG_FILE})')
        return

    # Print newest first
    records = list(reversed(records))
    total   = _count_all()
    shown   = len(records)
    caption = f'Showing {shown} of {total} experiments' if n else f'{total} experiments'

    # Column widths
    W_IDX  = 4
    W_TS   = 19   # YYYY-MM-DD HH:MM:SS
    W_STRAT = 14
    W_DATA  = 36  # SYMBOL INTERVAL START→END
    W_FOLDS = 5
    W_SHARP = 8
    W_PROF  = 6
    W_MED   = 5
    W_VERD  = 6

    sep = '─'
    hdr = (f'{"#":>{W_IDX}}  '
           f'{"Timestamp":<{W_TS}}  '
           f'{"Strategy":<{W_STRAT}}  '
           f'{"Data":<{W_DATA}}  '
           f'{"Fold":>{W_FOLDS}}  '
           f'{"Sharpe":>{W_SHARP}}  '
           f'{"Prof":>{W_PROF}}  '
           f'{"Med":>{W_MED}}  '
           f'{"Verdict":>{W_VERD}}')
    width = len(hdr)

    print(f'\n  {caption}  |  {LOG_FILE}')
    print(f'  {sep * width}')
    print(f'  {hdr}')
    print(f'  {sep * width}')

    total_records = _count_all()
    for i, r in enumerate(records):
        # Sequential number based on position in file (newest = highest)
        seq = total_records - i

        ts   = r.get('ts', '?')[:19].replace('T', ' ')
        strat = r.get('strategy', '?').upper()[:W_STRAT]

        d   = r.get('data', {})
        rng = f'{d.get("symbol","?")} {d.get("interval","?")}  {d.get("start","?")}→{d.get("end","?")}'
        rng = rng[:W_DATA]

        v   = r.get('validation', {})
        res = r.get('results', {})

        nf   = v.get('n_folds', '?')
        ms   = res.get('mean_sharpe', 0)
        prof = res.get('pct_profitable', 0)
        med  = res.get('median_trades', '?')
        verd = r.get('verdict', '?')

        prof_str = f'{prof * 100:.0f}%'
        ms_str   = f'{ms:+.2f}'
        verd_col = verd

        print(f'  {seq:>{W_IDX}}  '
              f'{ts:<{W_TS}}  '
              f'{strat:<{W_STRAT}}  '
              f'{rng:<{W_DATA}}  '
              f'{nf:>{W_FOLDS}}  '
              f'{ms_str:>{W_SHARP}}  '
              f'{prof_str:>{W_PROF}}  '
              f'{str(med):>{W_MED}}  '
              f'{verd_col:>{W_VERD}}')

    print(f'  {sep * width}')


def _count_all():
    """Return total number of valid records in the log."""
    if not os.path.exists(LOG_FILE):
        return 0
    count = 0
    with open(LOG_FILE, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                count += 1
    return count


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    argv = sys.argv[1:]
    n    = int(argv[0]) if argv and argv[0].isdigit() else None
    show(n)
