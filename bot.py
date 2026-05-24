#!/usr/bin/env python3
"""
Bitcoin Paper Trading Bot
Usage : python3 bot.py <strategy> <max_checks> <interval_seconds>
Example: python3 bot.py aggressive 336 3600   # 2 weeks, hourly
Delete state.json to start a fresh run.
"""

import json
import time
import logging
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Constants ──────────────────────────────────────────────────────────────────

STARTING_BALANCE = 1000.0          # EUR
MIN_TRADE_EUR    = 5.0             # skip trades smaller than this
STATE_FILE       = 'state.json'
TRADES_FILE      = 'trades.json'
LOG_FILE         = 'bot.log'
BINANCE_BASE     = 'https://api.binance.com/api/v3'

# ── Strategy Profiles ──────────────────────────────────────────────────────────
#
#  rsi_oversold   – buy  when RSI drops below this
#  rsi_overbought – sell when RSI rises above this
#  position_pct   – fraction of available cash (buy) or BTC (sell) to use
#  ma_short/long  – periods for the two simple moving averages
#  require_ma     – if True both RSI AND MA alignment must agree to trade

STRATEGIES = {
    'aggressive': {
        'rsi_oversold':   40,
        'rsi_overbought': 60,
        'position_pct':   0.90,
        'ma_short':       9,
        'ma_long':        21,
        'rsi_period':     14,
        'require_ma':     False,
        'desc': 'High-frequency, 90% positions, RSI-only, loose 40/60 thresholds',
    },
    'balanced': {
        'rsi_oversold':   35,
        'rsi_overbought': 65,
        'position_pct':   0.50,
        'ma_short':       20,
        'ma_long':        50,
        'rsi_period':     14,
        'require_ma':     True,
        'desc': 'Moderate frequency, 50% positions, RSI + MA alignment required',
    },
    'conservative': {
        'rsi_oversold':   30,
        'rsi_overbought': 70,
        'position_pct':   0.25,
        'ma_short':       20,
        'ma_long':        50,
        'rsi_period':     14,
        'require_ma':     True,
        'desc': 'Rare trades, 25% positions, strict RSI 30/70 + MA confirmation',
    },
}

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging():
    fmt = '%(asctime)s  %(levelname)-7s  %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )

log = logging.getLogger('btcbot')

# ── Binance Helpers ────────────────────────────────────────────────────────────

def _get_json(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'btcbot/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            log.warning(f'HTTP {e.code} on attempt {attempt}/{retries}: {url}')
        except Exception as e:
            log.warning(f'Network error attempt {attempt}/{retries}: {e}')
        if attempt < retries:
            time.sleep(5 * attempt)
    raise RuntimeError(f'Failed after {retries} attempts: {url}')

def detect_symbol():
    """Prefer BTCEUR; fall back to BTCUSDT if unavailable."""
    for sym in ('BTCEUR', 'BTCUSDT'):
        try:
            _get_json(f'{BINANCE_BASE}/ticker/price?symbol={sym}')
            return sym
        except Exception:
            continue
    raise RuntimeError('Cannot reach Binance API. Check your internet connection.')

def fetch_closes_kraken(limit=120):
    """Fallback price source — less IP-restrictive than Binance."""
    url = 'https://api.kraken.com/0/public/OHLC?pair=XBTEUR&interval=15'
    data = _get_json(url)
    if data.get('error'):
        raise RuntimeError(f'Kraken error: {data["error"]}')
    pair_key = next(k for k in data['result'] if k != 'last')
    return [float(c[4]) for c in data['result'][pair_key][-limit:]]

def fetch_closes(symbol, interval='15m', limit=120):
    """Return close prices — tries Binance first, falls back to Kraken."""
    try:
        url = f'{BINANCE_BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}'
        candles = _get_json(url, retries=2)
        return [float(c[4]) for c in candles]
    except Exception as e:
        log.warning(f'Binance unavailable ({e}), switching to Kraken')
        return fetch_closes_kraken(limit)

# ── Technical Indicators ───────────────────────────────────────────────────────

def calc_rsi(prices, period=14):
    """Wilder's smoothed RSI. Returns None if insufficient data."""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)

def calc_sma(prices, period):
    """Simple moving average of the last `period` prices. None if too short."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

# ── Signal Logic ───────────────────────────────────────────────────────────────

def get_signal(prices, strategy):
    """
    Returns (signal, indicators_dict).
    signal is one of: 'BUY', 'SELL', 'HOLD'
    """
    cfg    = STRATEGIES[strategy]
    needed = cfg['ma_long'] + cfg['rsi_period'] + 5
    if len(prices) < needed:
        return 'HOLD', {'note': f'warming up, need {needed} prices, have {len(prices)}'}

    r     = calc_rsi(prices, cfg['rsi_period'])
    ma_s  = calc_sma(prices, cfg['ma_short'])
    ma_l  = calc_sma(prices, cfg['ma_long'])

    bullish = ma_s is not None and ma_l is not None and ma_s > ma_l
    bearish = ma_s is not None and ma_l is not None and ma_s < ma_l

    signal = 'HOLD'
    if r is not None:
        if r < cfg['rsi_oversold']:
            if not cfg['require_ma'] or bullish:
                signal = 'BUY'
        elif r > cfg['rsi_overbought']:
            if not cfg['require_ma'] or bearish:
                signal = 'SELL'

    return signal, {
        'rsi':      round(r, 2) if r is not None else None,
        'ma_short': round(ma_s, 2) if ma_s else None,
        'ma_long':  round(ma_l, 2) if ma_l else None,
        'bullish':  bullish,
        'bearish':  bearish,
    }

# ── State Persistence ──────────────────────────────────────────────────────────

def load_state(strategy):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        saved_strat = state.get('strategy', strategy)
        if saved_strat != strategy:
            log.warning(
                f"State file uses strategy='{saved_strat}', not '{strategy}'. "
                "Continuing with saved strategy. Delete state.json to start fresh."
            )
        return state

    return {
        'strategy':     strategy,
        'symbol':       '',
        'balance':      STARTING_BALANCE,
        'btc_held':     0.0,
        'checks_done':  0,
        'total_trades': 0,
        'buys':         0,
        'sells':        0,
        'start_time':   datetime.now(timezone.utc).isoformat(),
        'last_check':   None,
        'last_price':   None,
        'peak_value':   STARTING_BALANCE,
        'trough_value': STARTING_BALANCE,
    }

def _atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)   # atomic on POSIX

def save_state(state):
    _atomic_write(STATE_FILE, state)

def append_trade(trade):
    trades = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, encoding='utf-8') as f:
            trades = json.load(f)
    trades.append(trade)
    _atomic_write(TRADES_FILE, trades)

# ── Trade Execution ────────────────────────────────────────────────────────────

def execute_trade(signal, state, price, cfg, check_num):
    """Mutates state in-place. Returns a trade dict or None."""
    ts = datetime.now(timezone.utc).isoformat()

    if signal == 'BUY' and state['balance'] >= MIN_TRADE_EUR:
        eur_in   = state['balance'] * cfg['position_pct']
        btc_out  = eur_in / price
        state['balance']  -= eur_in
        state['btc_held'] += btc_out
        state['total_trades'] += 1
        state['buys'] += 1
        log.info(f'  >> BUY  €{eur_in:>10,.2f} → {btc_out:.6f} BTC  @  €{price:,.2f}')
        return {
            'type': 'BUY', 'check': check_num, 'timestamp': ts,
            'price': price, 'eur_spent': round(eur_in, 4),
            'btc_bought': round(btc_out, 8),
            'balance_after': round(state['balance'], 4),
            'btc_after': round(state['btc_held'], 8),
        }

    if signal == 'SELL' and state['btc_held'] > 1e-9:
        btc_out  = state['btc_held'] * cfg['position_pct']
        eur_in   = btc_out * price
        state['btc_held'] -= btc_out
        state['balance']  += eur_in
        state['total_trades'] += 1
        state['sells'] += 1
        log.info(f'  >> SELL {btc_out:.6f} BTC → €{eur_in:>10,.2f}  @  €{price:,.2f}')
        return {
            'type': 'SELL', 'check': check_num, 'timestamp': ts,
            'price': price, 'eur_received': round(eur_in, 4),
            'btc_sold': round(btc_out, 8),
            'balance_after': round(state['balance'], 4),
            'btc_after': round(state['btc_held'], 8),
        }

    log.info(f'  >> HOLD')
    return None

# ── Main Loop ──────────────────────────────────────────────────────────────────

def print_summary(state, max_checks, interval):
    price_f  = state.get('last_price') or 0
    total    = state['balance'] + state['btc_held'] * price_f
    pnl      = total - STARTING_BALANCE
    pnl_pct  = pnl / STARTING_BALANCE * 100
    bar      = '=' * 64
    log.info(f'\n{bar}')
    log.info('FINAL SUMMARY')
    log.info(f'  Strategy     : {state["strategy"]}')
    log.info(f'  Symbol       : {state["symbol"]}')
    log.info(f'  Run          : {state["checks_done"]} checks x {interval}s')
    log.info(f'  Final value  : €{total:,.2f}')
    log.info(f'  P&L          : {pnl:+,.2f} EUR  ({pnl_pct:+.2f}%)')
    log.info(f'  Peak         : €{state["peak_value"]:,.2f}')
    log.info(f'  Trough       : €{state["trough_value"]:,.2f}')
    log.info(f'  Total trades : {state["total_trades"]}  '
             f'(buy={state["buys"]}  sell={state["sells"]})')
    log.info(f'  Cash left    : €{state["balance"]:,.2f}')
    log.info(f'  BTC held     : {state["btc_held"]:.8f}')
    log.info(bar)

def main():
    if len(sys.argv) < 4:
        print('Usage : python3 bot.py <strategy> <max_checks> <interval_seconds>')
        print('Example: python3 bot.py aggressive 336 3600')
        print(f'Strategies: {", ".join(STRATEGIES)}')
        sys.exit(1)

    arg_strategy = sys.argv[1].lower()
    max_checks   = int(sys.argv[2])
    interval     = int(sys.argv[3])

    if arg_strategy not in STRATEGIES:
        print(f'Unknown strategy "{arg_strategy}". Choose: {", ".join(STRATEGIES)}')
        sys.exit(1)

    setup_logging()

    state    = load_state(arg_strategy)
    strategy = state['strategy']          # honour saved strategy on resume
    cfg      = STRATEGIES[strategy]
    state['max_checks'] = max_checks      # always update so dashboard sees it
    state['interval']   = interval

    if not state.get('symbol'):
        state['symbol'] = detect_symbol()
        save_state(state)
    symbol = state['symbol']

    currency = 'EUR' if symbol.endswith('EUR') else 'USD'
    kline_limit = max(cfg['ma_long'] + cfg['rsi_period'] + 20, 120)

    bar = '=' * 64
    log.info(bar)
    log.info(f'Bitcoin Paper Trading Bot   strategy={strategy}   {symbol}')
    log.info(f'  {cfg["desc"]}')
    log.info(f'  RSI thresholds : buy<{cfg["rsi_oversold"]}  sell>{cfg["rsi_overbought"]}')
    log.info(f'  MA periods     : short={cfg["ma_short"]}  long={cfg["ma_long"]}')
    log.info(f'  Position size  : {int(cfg["position_pct"]*100)}%  |  MA confirm: {cfg["require_ma"]}')
    log.info(f'  Checks: {state["checks_done"]}/{max_checks}  |  Interval: {interval}s')
    log.info(f'  Starting balance : {STARTING_BALANCE} {currency}')
    log.info(f'  Current balance  : €{state["balance"]:.2f}  |  BTC: {state["btc_held"]:.8f}')
    log.info(bar)

    while state['checks_done'] < max_checks:
        check_num = state['checks_done'] + 1
        now_str   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log.info(f'\n-- Check {check_num}/{max_checks}  [{now_str}] --')

        try:
            prices = fetch_closes(symbol, limit=kline_limit)
            price  = prices[-1]

            signal, ind = get_signal(prices, strategy)

            log.info(
                f'  Price: €{price:>10,.2f}  |  RSI: {str(ind.get("rsi")):>6}  '
                f'|  MA{cfg["ma_short"]}: {str(ind.get("ma_short")):>10}  '
                f'|  MA{cfg["ma_long"]}: {str(ind.get("ma_long")):>10}  '
                f'|  Signal: {signal}'
            )

            portfolio = state['balance'] + state['btc_held'] * price
            log.info(
                f'  Portfolio: €{portfolio:>10,.2f}  '
                f'(cash €{state["balance"]:,.2f}  +  '
                f'{state["btc_held"]:.6f} BTC = €{state["btc_held"] * price:,.2f})'
            )

            trade = execute_trade(signal, state, price, cfg, check_num)
            if trade:
                append_trade(trade)

            portfolio_after = state['balance'] + state['btc_held'] * price
            state['peak_value']   = max(state['peak_value'],   portfolio_after)
            state['trough_value'] = min(state['trough_value'], portfolio_after)
            state['checks_done'] += 1
            state['last_check']  = datetime.now(timezone.utc).isoformat()
            state['last_price']  = price
            state['last_rsi']    = ind.get('rsi')
            save_state(state)

            pnl     = portfolio_after - STARTING_BALANCE
            pnl_pct = pnl / STARTING_BALANCE * 100
            log.info(f'  P&L: {pnl:+,.2f} {currency}  ({pnl_pct:+.2f}%)')

        except KeyboardInterrupt:
            log.info('\nInterrupted by user. Progress saved.')
            print_summary(state, max_checks, interval)
            sys.exit(0)
        except Exception as e:
            log.error(f'  Check failed: {e} — skipping, state NOT advanced')

        if state['checks_done'] < max_checks:
            if interval == 0:
                break   # run-once mode (used by GitHub Actions)
            wake_at = time.time() + interval
            log.info(f'  Sleeping {interval}s (next at {datetime.fromtimestamp(wake_at).strftime("%H:%M:%S")}) …')
            while time.time() < wake_at:
                time.sleep(10)

    print_summary(state, max_checks, interval)

if __name__ == '__main__':
    main()
