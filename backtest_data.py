#!/usr/bin/env python3
"""
Layer 1: OHLCV data pipeline.

Fetches historical candles from Binance /api/v3/klines with pagination and
caches them locally in .cache/{SYMBOL}_{interval}.json.  Re-runs load from
cache automatically; use --refresh to force a new API pull.

Cache format
------------
JSON file with a top-level '_meta' key holding provenance, then flat arrays:
  {
    "_meta": {"symbol","interval","candles","start","end","start_ms","end_ms","fetched_at"},
    "opens": [...], "highs": [...], "lows": [...],
    "closes": [...], "volumes": [...], "times": [... ms ...]
  }

Public API
----------
    fetch(symbol, interval, start_date, end_date, force_refresh) -> ohlcv dict
    info(symbol, interval)   -> prints cache metadata

CLI
---
    python3 backtest_data.py                         # info for ETHUSDT 4h
    python3 backtest_data.py BTCUSDT 1h 2021-01-01  # fetch/cache BTCUSDT 1h from 2021
    python3 backtest_data.py ETHUSDT 4h --refresh    # force re-fetch
"""

import sys, os, json, time, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import _get_json

BINANCE_BASE = 'https://api.binance.com/api/v3'
CACHE_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')

# Legacy cache written by the old backtest_research.py — migrated on first use.
_LEGACY_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'eth_4h_cache.json')


# ── helpers ────────────────────────────────────────────────────────────────────

def _ms(date_str):
    """'YYYY-MM-DD' → Unix milliseconds (UTC midnight)."""
    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(
        tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)

def _date(ms):
    """Unix milliseconds → 'YYYY-MM-DD'."""
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%d')

def _today_str():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')

def _cache_path(symbol, interval):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f'{symbol}_{interval}.json')


# ── API fetch ─────────────────────────────────────────────────────────────────

def _fetch_from_binance(symbol, interval, start_ms, end_ms):
    """
    Paginate Binance klines backwards from end_ms until start_ms is reached.
    Returns list of (openTime_ms, open, high, low, close, volume) tuples,
    oldest first.
    """
    all_candles = []
    cursor = end_ms
    call   = 0

    print(f'Fetching {symbol} {interval}  {_date(start_ms)} → {_date(end_ms)} ...')
    while cursor > start_ms:
        url = (f'{BINANCE_BASE}/klines?symbol={symbol}&interval={interval}'
               f'&limit=1000&endTime={cursor}')
        raw = _get_json(url)
        if not raw:
            break
        call += 1
        # Binance: [openTime, open, high, low, close, volume, closeTime, ...]
        batch = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                  float(r[4]), float(r[5])) for r in raw]
        all_candles = batch + all_candles   # prepend so oldest-first after loop
        print(f'  call {call:>2}: {_date(batch[0][0])} → {_date(batch[-1][0])}'
              f'  ({len(batch)} candles, {len(all_candles)} total)')
        if batch[0][0] <= start_ms:
            break
        cursor = batch[0][0] - 1
        time.sleep(0.12)                    # stay well under Binance rate limit

    # Deduplicate, filter, sort
    seen, uniq = set(), []
    for c in all_candles:
        if c[0] not in seen and c[0] >= start_ms:
            seen.add(c[0])
            uniq.append(c)
    uniq.sort(key=lambda x: x[0])
    return uniq


# ── cache helpers ─────────────────────────────────────────────────────────────

def _candles_to_ohlcv(candles, symbol, interval):
    """Convert list of tuples to the standard cache dict (including _meta)."""
    return {
        '_meta': {
            'symbol':     symbol,
            'interval':   interval,
            'candles':    len(candles),
            'start':      _date(candles[0][0]),
            'end':        _date(candles[-1][0]),
            'start_ms':   candles[0][0],
            'end_ms':     candles[-1][0],
            'fetched_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        },
        'opens':   [c[1] for c in candles],
        'highs':   [c[2] for c in candles],
        'lows':    [c[3] for c in candles],
        'closes':  [c[4] for c in candles],
        'volumes': [c[5] for c in candles],
        'times':   [c[0] for c in candles],
    }

def _drop_meta(ohlcv):
    """Return a copy of ohlcv without the _meta key."""
    return {k: v for k, v in ohlcv.items() if k != '_meta'}

def _slice(ohlcv, start_ms, end_ms):
    """Return ohlcv arrays sliced to [start_ms, end_ms] inclusive."""
    times = ohlcv['times']
    lo = next((i for i, t in enumerate(times) if t >= start_ms), 0)
    hi = next((i for i, t in enumerate(times) if t >  end_ms),  len(times))
    keys = ('opens', 'highs', 'lows', 'closes', 'volumes', 'times')
    return {k: ohlcv[k][lo:hi] for k in keys}

def _print_meta(meta, path=''):
    n    = meta.get('candles', '?')
    rng  = f'{meta.get("start", "?")} → {meta.get("end", "?")}'
    when = meta.get('fetched_at', '?')
    tail = f'  ({path})' if path else ''
    print(f'  {meta.get("symbol")} {meta.get("interval")}  '
          f'{n} candles  {rng}  fetched {when}{tail}')

def _try_migrate_legacy(symbol, interval, path):
    """
    One-time migration: if the old eth_4h_cache.json exists and the new cache
    doesn't, copy it over with _meta added.  Returns True if migrated.
    """
    if symbol != 'ETHUSDT' or interval != '4h':
        return False
    if not os.path.exists(_LEGACY_CACHE):
        return False
    print(f'Migrating legacy cache {_LEGACY_CACHE} → {path}')
    with open(_LEGACY_CACHE) as f:
        old = json.load(f)
    times = old.get('times', [])
    if not times:
        return False
    # Build candle tuples to go through the standard converter
    n_c = len(old['closes'])
    candles = [(old['times'][i], old['opens'][i], old['highs'][i],
                old['lows'][i],  old['closes'][i], old['volumes'][i])
               for i in range(n_c)]
    ohlcv = _candles_to_ohlcv(candles, symbol, interval)
    ohlcv['_meta']['fetched_at'] = 'migrated-from-eth_4h_cache.json'
    with open(path, 'w') as f:
        json.dump(ohlcv, f)
    _print_meta(ohlcv['_meta'], path)
    return True


# ── public API ────────────────────────────────────────────────────────────────

def fetch(symbol='ETHUSDT', interval='4h',
          start_date=None, end_date=None, force_refresh=False):
    """
    Return ohlcv dict for (symbol, interval) covering [start_date, end_date].

    Parameters
    ----------
    symbol       : Binance pair, e.g. 'ETHUSDT', 'BTCUSDT'
    interval     : Binance interval string, e.g. '4h', '1h', '1d'
    start_date   : 'YYYY-MM-DD'; defaults to '2020-01-01'
    end_date     : 'YYYY-MM-DD'; defaults to today
    force_refresh: ignore cache and re-fetch from Binance

    Returns
    -------
    dict with keys: opens, highs, lows, closes, volumes, times(ms)
    All lists are oldest-first.  No _meta key in the returned dict.
    """
    start_str = start_date or '2020-01-01'
    end_str   = end_date   or _today_str()
    start_ms  = _ms(start_str)
    end_ms    = _ms(end_str) + 86_400_000   # inclusive of end day

    path = _cache_path(symbol, interval)

    if not force_refresh:
        # Try migration of legacy cache (one-time, no-op after first run)
        if not os.path.exists(path):
            _try_migrate_legacy(symbol, interval, path)

        if os.path.exists(path):
            with open(path) as f:
                cached = json.load(f)
            meta = cached.get('_meta', {})
            cache_start = meta.get('start_ms', 0)
            cache_end   = meta.get('end_ms',   0)

            if cache_start <= start_ms and cache_end >= start_ms:
                print(f'Cache hit ', end='')
                _print_meta(meta)
                sliced = _slice(cached, start_ms, end_ms)
                if not sliced['closes']:
                    raise RuntimeError(
                        f'Cache covers {meta.get("start")}–{meta.get("end")} but the '
                        f'requested slice {start_str}–{end_str} is empty. '
                        f'Use force_refresh=True or adjust dates.')
                return sliced
            else:
                print(f'Cache present but range mismatch '
                      f'(need from {start_str}, cache starts {meta.get("start", "?")}). '
                      f'Re-fetching.')

    # Fetch from Binance
    now_ms  = int(time.time() * 1000)
    candles = _fetch_from_binance(symbol, interval, start_ms, min(end_ms, now_ms))
    if not candles:
        raise RuntimeError(f'Binance returned no data for {symbol} {interval}')

    ohlcv = _candles_to_ohlcv(candles, symbol, interval)
    with open(path, 'w') as f:
        json.dump(ohlcv, f)
    print(f'Saved  ', end='')
    _print_meta(ohlcv['_meta'], path)
    return _drop_meta(ohlcv)


def info(symbol='ETHUSDT', interval='4h'):
    """Print cache metadata for the given (symbol, interval) pair."""
    path = _cache_path(symbol, interval)
    if not os.path.exists(path):
        print(f'No cache for {symbol} {interval}  ({path})')
        return
    with open(path) as f:
        cached = json.load(f)
    meta = cached.get('_meta')
    if meta:
        _print_meta(meta, path)
    else:
        print(f'{path}: no _meta key (legacy format)')


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    argv    = sys.argv[1:]
    refresh = '--refresh' in argv
    args    = [a for a in argv if not a.startswith('--')]

    sym = args[0].upper() if len(args) > 0 else 'ETHUSDT'
    iv  = args[1].lower() if len(args) > 1 else '4h'
    sd  = args[2]         if len(args) > 2 else None
    ed  = args[3]         if len(args) > 3 else None

    info(sym, iv)
    result = fetch(sym, iv, sd, ed, force_refresh=refresh)
    n = len(result['closes'])
    t0 = _date(result['times'][0])
    t1 = _date(result['times'][-1])
    print(f'Returned {n} candles  {t0} → {t1}')
