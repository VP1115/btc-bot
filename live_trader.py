#!/usr/bin/env python3
"""
Live Trader — executes real orders via ccxt.
NOT activated by default. Only runs with --live flag.

Setup:
  pip install ccxt python-dotenv
  cp .env.example .env
  # fill in your exchange API keys in .env

Usage:
  python3 live_trader.py --dry-run    # simulate without touching exchange
  python3 live_trader.py --live       # real money — confirm first 10 trades
  python3 live_trader.py --live --liquidate  # close all positions immediately

.env format:
  EXCHANGE=binance
  API_KEY=your_key_here
  API_SECRET=your_secret_here
  PAPER=false
"""

import sys, os, json, time, logging
from datetime import datetime, timezone

# Guard: refuse to run without explicit --live flag
if '--live' not in sys.argv and '--dry-run' not in sys.argv:
    print('live_trader.py requires --live or --dry-run flag.')
    print('This file executes REAL orders. Read the source before using.')
    sys.exit(1)

LIVE_MODE = '--live' in sys.argv
DRY_RUN   = '--dry-run' in sys.argv or not LIVE_MODE

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    handlers=[
        logging.FileHandler('live_trader.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('live')

# ── Confirmation counter (first 10 live trades require user confirmation) ──────
CONFIRM_FILE = '.live_confirms_done'

def _confirms_done():
    try:
        return int(open(CONFIRM_FILE).read().strip())
    except Exception:
        return 0

def _increment_confirms():
    n = _confirms_done() + 1
    with open(CONFIRM_FILE, 'w') as f:
        f.write(str(n))
    return n

def require_confirmation(action, symbol, amount, price):
    done = _confirms_done()
    if done >= 10:
        return True
    ans = input(f'\n!! LIVE ORDER #{done+1}/10: {action} {amount:.6f} {symbol} @ ~{price:.2f}  [y/N] ').strip().lower()
    if ans == 'y':
        _increment_confirms()
        return True
    log.warning('Order cancelled by user.')
    return False

# ── Exchange setup ─────────────────────────────────────────────────────────────

def load_exchange():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        import ccxt
    except ImportError:
        print('ccxt not installed. Run: pip install ccxt python-dotenv')
        sys.exit(1)

    exchange_id = os.getenv('EXCHANGE', 'binance')
    api_key     = os.getenv('API_KEY', '')
    api_secret  = os.getenv('API_SECRET', '')

    if not api_key or not api_secret:
        print('API_KEY and API_SECRET must be set in .env')
        sys.exit(1)

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        'apiKey':    api_key,
        'secret':    api_secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'},
    })

    log.info(f'Exchange: {exchange_id}  live={LIVE_MODE}')
    return exchange

# ── Order execution ────────────────────────────────────────────────────────────

def place_order(exchange, order_type, symbol, amount_eur, price, dry_run=True):
    """
    Places a limit order. Returns order dict or None on failure.
    symbol: e.g. 'BTC/EUR'
    """
    if amount_eur < 5:
        log.warning(f'Order too small: €{amount_eur:.2f} < €5 minimum')
        return None

    amount_coin = amount_eur / price

    log.info(f'  ORDER: {order_type} {amount_coin:.6f} {symbol} @ {price:.2f}  (€{amount_eur:.2f})')

    if dry_run:
        log.info('  [DRY RUN — no order placed]')
        return {'id': 'dry-run', 'status': 'simulated', 'amount': amount_coin, 'price': price}

    if not require_confirmation(order_type, symbol, amount_coin, price):
        return None

    try:
        if order_type == 'BUY':
            order = exchange.create_limit_buy_order(symbol, amount_coin, price)
        else:
            order = exchange.create_limit_sell_order(symbol, amount_coin, price)

        log.info(f'  Order placed: id={order["id"]}  status={order["status"]}')
        _log_api_call('place_order', {'type': order_type, 'symbol': symbol,
                                      'amount': amount_coin, 'price': price}, order)
        return order
    except Exception as e:
        log.error(f'  Order failed: {e}')
        return None

def _log_api_call(action, params, response):
    entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'action':    action,
        'params':    params,
        'response':  str(response)[:500],
    }
    path = 'live_api_log.json'
    calls = []
    if os.path.exists(path):
        try:
            calls = json.load(open(path))
        except Exception:
            pass
    calls.append(entry)
    calls = calls[-1000:]
    with open(path, 'w') as f:
        json.dump(calls, f, indent=2)

# ── Liquidate all positions ────────────────────────────────────────────────────

def liquidate_all(exchange, symbols=('BTC/EUR', 'ETH/EUR')):
    log.warning('!! LIQUIDATING ALL POSITIONS !!')
    for symbol in symbols:
        try:
            balance  = exchange.fetch_balance()
            base     = symbol.split('/')[0]
            held     = balance['free'].get(base, 0)
            if held < 1e-8:
                log.info(f'  {symbol}: nothing held')
                continue
            ticker   = exchange.fetch_ticker(symbol)
            price    = ticker['bid']
            log.info(f'  Liquidating {held:.6f} {base} @ {price:.2f}')
            if LIVE_MODE:
                exchange.create_market_sell_order(symbol, held)
            else:
                log.info('  [DRY RUN]')
        except Exception as e:
            log.error(f'  Liquidate {symbol} failed: {e}')

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f'live_trader.py  live={LIVE_MODE}  dry_run={DRY_RUN}')

    exchange = load_exchange()

    if '--liquidate' in sys.argv:
        liquidate_all(exchange)
        return

    # Example: read bot state and mirror signals to exchange
    # This is intentionally left as a scaffold — wire it to bot.py signals.
    log.info('live_trader.py scaffold loaded. Wire to bot.py get_signal() to enable.')
    log.info('Example: watch state_BTC.json for last_signal changes and mirror to exchange.')

if __name__ == '__main__':
    main()
