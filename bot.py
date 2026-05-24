#!/usr/bin/env python3
"""
Crypto Paper Trading Bot
Usage : python3 bot.py <strategy> <max_checks> <interval_seconds> [name] [starting_balance]
Example: python3 bot.py aggressive 4032 0 BTC 2000
         python3 bot.py balanced   4032 0 ETH 2000
         python3 bot.py aggressive 4032 0 SOL 1000
Delete state_<NAME>.json to start that asset fresh.
"""

import json
import time
import logging
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Strategy Profiles ──────────────────────────────────────────────────────────

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

BINANCE_BASE = 'https://api.binance.com/api/v3'
MIN_TRADE_EUR = 5.0

KRAKEN_PAIR_MAP = {
    'BTCEUR':  'XBTEUR',  'BTCUSDT':  'XBTUSD',
    'ETHEUR':  'ETHEUR',  'ETHUSDT':  'ETHUSD',
}

COINGECKO_IDS = {
    'BTCEUR': 'bitcoin',   'BTCUSDT': 'bitcoin',
    'ETHEUR': 'ethereum',  'ETHUSDT': 'ethereum',
    'SOLEUR': 'solana',    'SOLUSDT': 'solana',
    'BNBEUR': 'binancecoin', 'ADAEUR': 'cardano',
}

# Set dynamically in main()
STATE_FILE  = 'state_BTC.json'
TRADES_FILE = 'trades_BTC.json'
LOG_FILE    = 'bot.log'

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(name):
    global LOG_FILE
    LOG_FILE = f'bot_{name}.log'
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

# ── API Helpers ────────────────────────────────────────────────────────────────

def _get_json(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'btcbot/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            log.warning(f'HTTP {e.code} attempt {attempt}/{retries}')
        except Exception as e:
            log.warning(f'Network error attempt {attempt}/{retries}: {e}')
        if attempt < retries:
            time.sleep(5 * attempt)
    raise RuntimeError(f'Failed after {retries} attempts: {url}')

def detect_symbol(name='BTC'):
    candidates = {
        'BTC': ('BTCEUR', 'BTCUSDT'),
        'ETH': ('ETHEUR', 'ETHUSDT'),
        'SOL': ('SOLEUR', 'SOLUSDT'),
        'BNB': ('BNBEUR', 'BNBUSDT'),
        'ADA': ('ADAEUR', 'ADAUSDT'),
    }.get(name.upper(), (f'{name}EUR', f'{name}USDT'))

    for sym in candidates:
        try:
            _get_json(f'{BINANCE_BASE}/ticker/price?symbol={sym}')
            log.info(f'Symbol detected: {sym}')
            return sym
        except Exception:
            continue
    return f'{name}EUR'

def fetch_closes_kraken(symbol, limit=120):
    pair = KRAKEN_PAIR_MAP.get(symbol)
    if not pair:
        raise RuntimeError(f'No Kraken mapping for {symbol}')
    url  = f'https://api.kraken.com/0/public/OHLC?pair={pair}&interval=15'
    data = _get_json(url)
    if data.get('error') and data['error']:
        raise RuntimeError(f'Kraken: {data["error"]}')
    pair_key = next(k for k in data['result'] if k != 'last')
    return [float(c[4]) for c in data['result'][pair_key][-limit:]]

def fetch_closes_coingecko(symbol, limit=120):
    coin_id  = COINGECKO_IDS.get(symbol)
    if not coin_id:
        raise RuntimeError(f'No CoinGecko mapping for {symbol}')
    currency = 'eur' if symbol.endswith('EUR') else 'usd'
    url  = f'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency={currency}&days=7&interval=hourly'
    data = _get_json(url)
    return [p[1] for p in data['prices'][-limit:]]

def fetch_closes(symbol, limit=120):
    for source, fn in [
        ('Binance',    lambda: [float(c[4]) for c in _get_json(f'{BINANCE_BASE}/klines?symbol={symbol}&interval=15m&limit={limit}', retries=2)]),
        ('Kraken',     lambda: fetch_closes_kraken(symbol, limit)),
        ('CoinGecko',  lambda: fetch_closes_coingecko(symbol, limit)),
    ]:
        try:
            prices = fn()
            if prices:
                if source != 'Binance':
                    log.info(f'Using {source} for {symbol}')
                return prices
        except Exception as e:
            log.warning(f'{source} failed for {symbol}: {e}')
    raise RuntimeError(f'All price sources failed for {symbol}')

# ── Technical Indicators ───────────────────────────────────────────────────────

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)

def calc_sma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def get_signal(prices, strategy):
    cfg    = STRATEGIES[strategy]
    needed = cfg['ma_long'] + cfg['rsi_period'] + 5
    if len(prices) < needed:
        return 'HOLD', {'note': f'warming up ({len(prices)}/{needed})'}

    r    = calc_rsi(prices, cfg['rsi_period'])
    ma_s = calc_sma(prices, cfg['ma_short'])
    ma_l = calc_sma(prices, cfg['ma_long'])

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

def load_state(strategy, starting_balance):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        if state.get('strategy') != strategy:
            log.warning(f"Saved strategy={state['strategy']} differs from arg={strategy}. Using saved.")
        return state

    return {
        'strategy':         strategy,
        'symbol':           '',
        'balance':          starting_balance,
        'coin_held':        0.0,
        'checks_done':      0,
        'total_trades':     0,
        'buys':             0,
        'sells':            0,
        'start_time':       datetime.now(timezone.utc).isoformat(),
        'last_check':       None,
        'last_price':       None,
        'last_rsi':         None,
        'peak_value':       starting_balance,
        'trough_value':     starting_balance,
        'starting_balance': starting_balance,
    }

def _atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

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
    ts = datetime.now(timezone.utc).isoformat()

    if signal == 'BUY' and state['balance'] >= MIN_TRADE_EUR:
        eur_in   = state['balance'] * cfg['position_pct']
        coin_out = eur_in / price
        state['balance']   -= eur_in
        state['coin_held'] += coin_out
        state['total_trades'] += 1
        state['buys'] += 1
        log.info(f'  >> BUY  €{eur_in:>10,.2f} → {coin_out:.6f}  @  €{price:,.2f}')
        return {
            'type': 'BUY', 'check': check_num, 'timestamp': ts,
            'price': price, 'eur_spent': round(eur_in, 4),
            'coin_bought': round(coin_out, 8),
            'balance_after': round(state['balance'], 4),
            'coin_after': round(state['coin_held'], 8),
        }

    if signal == 'SELL' and state['coin_held'] > 1e-9:
        coin_out = state['coin_held'] * cfg['position_pct']
        eur_in   = coin_out * price
        state['coin_held'] -= coin_out
        state['balance']   += eur_in
        state['total_trades'] += 1
        state['sells'] += 1
        log.info(f'  >> SELL {coin_out:.6f} → €{eur_in:>10,.2f}  @  €{price:,.2f}')
        return {
            'type': 'SELL', 'check': check_num, 'timestamp': ts,
            'price': price, 'eur_received': round(eur_in, 4),
            'coin_sold': round(coin_out, 8),
            'balance_after': round(state['balance'], 4),
            'coin_after': round(state['coin_held'], 8),
        }

    log.info('  >> HOLD')
    return None

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global STATE_FILE, TRADES_FILE

    if len(sys.argv) < 4:
        print('Usage: python3 bot.py <strategy> <max_checks> <interval> [name] [starting_balance]')
        sys.exit(1)

    arg_strategy      = sys.argv[1].lower()
    max_checks        = int(sys.argv[2])
    interval          = int(sys.argv[3])
    name              = sys.argv[4].upper() if len(sys.argv) > 4 else 'BTC'
    starting_balance  = float(sys.argv[5]) if len(sys.argv) > 5 else 1000.0

    if arg_strategy not in STRATEGIES:
        print(f'Unknown strategy. Choose: {", ".join(STRATEGIES)}')
        sys.exit(1)

    STATE_FILE  = f'state_{name}.json'
    TRADES_FILE = f'trades_{name}.json'

    setup_logging(name)

    state    = load_state(arg_strategy, starting_balance)
    strategy = state['strategy']
    cfg      = STRATEGIES[strategy]
    start    = state.get('starting_balance', starting_balance)

    state['max_checks'] = max_checks
    if interval > 0:
        state['interval'] = interval

    # Migrate old 'btc_held' key to generic 'coin_held'
    if 'btc_held' in state and 'coin_held' not in state:
        state['coin_held'] = state.pop('btc_held')

    if not state.get('symbol'):
        state['symbol'] = detect_symbol(name)
        save_state(state)
    symbol = state['symbol']

    kline_limit = max(cfg['ma_long'] + cfg['rsi_period'] + 20, 120)

    bar = '=' * 64
    log.info(bar)
    log.info(f'Crypto Paper Trading Bot  [{name}]  strategy={strategy}  {symbol}')
    log.info(f'  {cfg["desc"]}')
    log.info(f'  Budget: €{start:,.0f}  |  Checks: {state["checks_done"]}/{max_checks}')
    log.info(f'  Cash: €{state["balance"]:.2f}  |  Held: {state["coin_held"]:.6f}')
    log.info(bar)

    while state['checks_done'] < max_checks:
        check_num = state['checks_done'] + 1
        log.info(f'\n-- [{name}] Check {check_num}/{max_checks}  [{datetime.now().strftime("%H:%M:%S")}] --')

        try:
            prices = fetch_closes(symbol, limit=kline_limit)
            price  = prices[-1]
            signal, ind = get_signal(prices, strategy)

            log.info(
                f'  Price: €{price:>10,.2f}  |  RSI: {str(ind.get("rsi")):>6}  '
                f'|  MA{cfg["ma_short"]}: {str(ind.get("ma_short")):>10}  '
                f'|  Signal: {signal}'
            )

            portfolio = state['balance'] + state['coin_held'] * price
            log.info(f'  Portfolio: €{portfolio:>10,.2f}  (cash €{state["balance"]:,.2f} + coin €{state["coin_held"]*price:,.2f})')

            trade = execute_trade(signal, state, price, cfg, check_num)
            if trade:
                append_trade(trade)

            portfolio_after       = state['balance'] + state['coin_held'] * price
            state['peak_value']   = max(state['peak_value'],   portfolio_after)
            state['trough_value'] = min(state['trough_value'], portfolio_after)
            state['checks_done'] += 1
            state['last_check']  = datetime.now(timezone.utc).isoformat()
            state['last_price']  = price
            state['last_rsi']    = ind.get('rsi')
            save_state(state)

            pnl     = portfolio_after - start
            pnl_pct = pnl / start * 100
            log.info(f'  P&L: {pnl:+,.2f} EUR  ({pnl_pct:+.2f}%)')

        except KeyboardInterrupt:
            log.info('Interrupted. Progress saved.')
            sys.exit(0)
        except Exception as e:
            log.error(f'  Check failed: {e}')

        if state['checks_done'] < max_checks:
            if interval == 0:
                break
            wake_at = time.time() + interval
            log.info(f'  Sleeping {interval}s (next at {datetime.fromtimestamp(wake_at).strftime("%H:%M:%S")}) …')
            while time.time() < wake_at:
                time.sleep(10)

    price_f = state.get('last_price') or 0
    total   = state['balance'] + state['coin_held'] * price_f
    pnl     = total - start
    log.info(f'\n[{name}] DONE  value=€{total:,.2f}  P&L={pnl:+,.2f} EUR  trades={state["total_trades"]}')

if __name__ == '__main__':
    main()
