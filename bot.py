#!/usr/bin/env python3
"""
Crypto Paper Trading Bot v2
Usage: python3 bot.py <strategy> <max_checks> <interval_seconds> [name] [starting_balance]
       python3 bot.py aggressive 4032 0 BTC 2000
       python3 bot.py aggressive 4032 0 ETH 2000
       python3 bot.py aggressive 4032 0 SOL 1000
Create a file named STOP to halt the bot cleanly.
"""

import json, time, logging, sys, os, math
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── Strategy profiles ──────────────────────────────────────────────────────────

STRATEGIES = {
    'aggressive': {
        'rsi_oversold':    40,
        'rsi_overbought':  60,
        'ma_short':         9,
        'ma_long':         21,
        'rsi_period':      14,
        'stop_loss_pct':  0.05,   # hard stop  -5%
        'take_profit_pct':0.10,   # hard TP   +10%
        'trail_trigger':  0.03,   # arm trail after +3%
        'trail_pct':      0.02,   # trail 2% below peak
        'risk_pct':       0.02,   # risk 2% of portfolio per trade
        'max_pos_pct':    0.90,   # cap at 90% of cash
        'adx_min':        20,
        'desc': 'Aggressive: ATR-sized positions, trailing stops, multi-indicator',
    },
    'balanced': {
        'rsi_oversold':    35,
        'rsi_overbought':  65,
        'ma_short':        20,
        'ma_long':         50,
        'rsi_period':      14,
        'stop_loss_pct':  0.03,
        'take_profit_pct':0.08,
        'trail_trigger':  0.03,
        'trail_pct':      0.02,
        'risk_pct':       0.015,
        'max_pos_pct':    0.50,
        'adx_min':        25,
        'desc': 'Balanced: 50% max position, tighter stop/TP, ADX25 trend filter',
    },
    'conservative': {
        'rsi_oversold':    30,
        'rsi_overbought':  70,
        'ma_short':        20,
        'ma_long':         50,
        'rsi_period':      14,
        'stop_loss_pct':  0.02,
        'take_profit_pct':0.06,
        'trail_trigger':  0.02,
        'trail_pct':      0.015,
        'risk_pct':       0.01,
        'max_pos_pct':    0.25,
        'adx_min':        25,
        'desc': 'Conservative: 25% max, tight stops, strict RSI 30/70',
    },
}

BINANCE_BASE  = 'https://api.binance.com/api/v3'
MIN_TRADE_EUR = 5.0
STOP_FILE     = 'STOP'
PAUSE_FILE    = 'PAUSE'

KRAKEN_PAIR_MAP = {
    'BTCEUR': 'XBTEUR', 'BTCUSDT': 'XBTUSD',
    'ETHEUR': 'ETHEUR', 'ETHUSDT': 'ETHUSD',
}

COINGECKO_IDS = {
    'BTCEUR': 'bitcoin',     'BTCUSDT': 'bitcoin',
    'ETHEUR': 'ethereum',    'ETHUSDT': 'ethereum',
    'SOLEUR': 'solana',      'SOLUSDT': 'solana',
    'BNBEUR': 'binancecoin', 'ADAEUR':  'cardano',
}

# Set dynamically in main()
STATE_FILE  = 'state_BTC.json'
TRADES_FILE = 'trades_BTC.json'
LOG_FILE    = 'bot.log'

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(name):
    global LOG_FILE
    LOG_FILE = f'bot_{name}.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )

log = logging.getLogger('btcbot')

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get_json(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'btcbot/2.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            log.warning(f'HTTP {e.code} attempt {attempt}/{retries}')
        except Exception as e:
            log.warning(f'Network error attempt {attempt}/{retries}: {e}')
        if attempt < retries:
            time.sleep(5 * attempt)
    raise RuntimeError(f'Failed after {retries} attempts: {url}')

# ── OHLCV fetchers ─────────────────────────────────────────────────────────────
# Each returns dict: {opens, highs, lows, closes, volumes} — lists, oldest first

def _pack(rows):
    return {
        'opens':   [float(r[0]) for r in rows],
        'highs':   [float(r[1]) for r in rows],
        'lows':    [float(r[2]) for r in rows],
        'closes':  [float(r[3]) for r in rows],
        'volumes': [float(r[4]) for r in rows],
    }

def _closes_to_ohlcv(closes):
    """Fallback when only closes are available."""
    return {
        'opens':   closes[:],
        'highs':   [c * 1.003 for c in closes],
        'lows':    [c * 0.997 for c in closes],
        'closes':  closes[:],
        'volumes': [1.0] * len(closes),
    }

def detect_symbol(name='BTC'):
    candidates = {
        'BTC': ('BTCEUR', 'BTCUSDT'), 'ETH': ('ETHEUR', 'ETHUSDT'),
        'SOL': ('SOLEUR', 'SOLUSDT'), 'BNB': ('BNBEUR', 'BNBUSDT'),
        'ADA': ('ADAEUR', 'ADAUSDT'),
    }.get(name.upper(), (f'{name}EUR', f'{name}USDT'))
    for sym in candidates:
        try:
            _get_json(f'{BINANCE_BASE}/ticker/price?symbol={sym}', retries=1)
            log.info(f'Symbol detected: {sym}')
            return sym
        except Exception:
            continue
    return f'{name}EUR'

def _fetch_binance(symbol, limit):
    raw = _get_json(f'{BINANCE_BASE}/klines?symbol={symbol}&interval=15m&limit={limit}', retries=2)
    return _pack([(c[1], c[2], c[3], c[4], c[5]) for c in raw])

def _fetch_kraken(symbol, limit):
    pair = KRAKEN_PAIR_MAP.get(symbol)
    if not pair:
        raise RuntimeError(f'No Kraken mapping for {symbol}')
    data = _get_json(f'https://api.kraken.com/0/public/OHLC?pair={pair}&interval=15')
    if data.get('error') and data['error']:
        raise RuntimeError(f'Kraken: {data["error"]}')
    key = next(k for k in data['result'] if k != 'last')
    rows = data['result'][key][-limit:]
    return _pack([(c[1], c[2], c[3], c[4], c[6]) for c in rows])

def _fetch_coingecko(symbol, limit):
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        raise RuntimeError(f'No CoinGecko mapping for {symbol}')
    currency = 'eur' if symbol.endswith('EUR') else 'usd'
    data = _get_json(f'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency={currency}&days=7')
    return _closes_to_ohlcv([p[1] for p in data['prices'][-limit:]])

def _fetch_cryptocompare(symbol, limit):
    for suffix in ('EUR', 'USDT', 'USD'):
        if symbol.endswith(suffix):
            fsym = symbol[:-len(suffix)]
            tsym = suffix if suffix in ('EUR', 'USD') else 'USD'
            break
    else:
        raise RuntimeError(f'Cannot parse symbol: {symbol}')
    data = _get_json(f'https://min-api.cryptocompare.com/data/v2/histohour?fsym={fsym}&tsym={tsym}&limit={limit}')
    if data.get('Response') != 'Success':
        raise RuntimeError(f'CryptoCompare: {data.get("Message")}')
    c = data['Data']['Data']
    return _pack([(x['open'], x['high'], x['low'], x['close'], x['volumefrom']) for x in c])

def fetch_ohlcv(symbol, limit=200):
    for source, fn in [
        ('Binance',       lambda: _fetch_binance(symbol, limit)),
        ('Kraken',        lambda: _fetch_kraken(symbol, limit)),
        ('CoinGecko',     lambda: _fetch_coingecko(symbol, limit)),
        ('CryptoCompare', lambda: _fetch_cryptocompare(symbol, limit)),
    ]:
        try:
            d = fn()
            if d['closes']:
                if source != 'Binance':
                    log.info(f'Using {source} for {symbol}')
                return d
        except Exception as e:
            log.warning(f'{source} failed: {e}')
    raise RuntimeError(f'All price sources failed for {symbol}')

# ── Technical indicators ───────────────────────────────────────────────────────

def _ema_series(data, period):
    if len(data) < period:
        return []
    k   = 2.0 / (period + 1)
    out = [sum(data[:period]) / period]
    for x in data[period:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    d = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = [max(x, 0.0) for x in d]
    l = [max(-x, 0.0) for x in d]
    ag = sum(g[:period]) / period
    al = sum(l[:period]) / period
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

def calc_sma(data, period):
    if len(data) < period:
        return None
    return sum(data[-period:]) / period

def calc_macd(closes, fast=12, slow=26, sig=9):
    """Returns (macd, signal, hist, bull_cross, bear_cross)."""
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    if not ef or not es:
        return None, None, None, False, False
    offset    = slow - fast
    macd_line = [ef[i + offset] - es[i] for i in range(len(es))]
    sig_line  = _ema_series(macd_line, sig)
    if len(sig_line) < 2:
        return None, None, None, False, False
    h_now  = macd_line[-1] - sig_line[-1]
    h_prev = macd_line[-2] - sig_line[-2]
    return macd_line[-1], sig_line[-1], h_now, h_prev <= 0 < h_now, h_prev >= 0 > h_now

def calc_bbands(closes, period=20, num_std=2.0):
    if len(closes) < period:
        return None, None, None
    w   = closes[-period:]
    mid = sum(w) / period
    std = (sum((p - mid) ** 2 for p in w) / period) ** 0.5
    return mid - num_std * std, mid, mid + num_std * std

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i-1]),
               abs(lows[i]  - closes[i-1])) for i in range(1, len(closes))]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def calc_adx(highs, lows, closes, period=14):
    """Returns (adx, plus_di, minus_di)."""
    if len(closes) < period * 2 + 2:
        return None, None, None
    pdm, mdm, trs = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i-1]
        dn   = lows[i-1] - lows[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr_w = sum(trs[:period]) / period
    p_w   = sum(pdm[:period]) / period
    m_w   = sum(mdm[:period]) / period
    dx_vals = []
    for i in range(period, len(trs)):
        atr_w = (atr_w * (period-1) + trs[i]) / period
        p_w   = (p_w   * (period-1) + pdm[i]) / period
        m_w   = (m_w   * (period-1) + mdm[i]) / period
        pdi = 100.0 * p_w / atr_w if atr_w > 0 else 0.0
        mdi = 100.0 * m_w / atr_w if atr_w > 0 else 0.0
        s   = pdi + mdi
        dx_vals.append(100.0 * abs(pdi - mdi) / s if s > 0 else 0.0)
    if len(dx_vals) < period:
        return None, None, None
    adx = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx = (adx * (period-1) + dx) / period
    pdi_f = 100.0 * p_w / atr_w if atr_w > 0 else 0.0
    mdi_f = 100.0 * m_w / atr_w if atr_w > 0 else 0.0
    return adx, pdi_f, mdi_f

def calc_vwma(closes, volumes, period=20):
    if len(closes) < period:
        return None
    c, v = closes[-period:], volumes[-period:]
    tv   = sum(v)
    return sum(c[i] * v[i] for i in range(period)) / tv if tv > 0 else None

def compute_indicators(ohlcv):
    closes  = ohlcv['closes']
    highs   = ohlcv['highs']
    lows    = ohlcv['lows']
    volumes = ohlcv['volumes']
    price   = closes[-1]

    rsi                              = calc_rsi(closes, 14)
    ma_s                             = calc_sma(closes, 9)
    ma_l                             = calc_sma(closes, 21)
    macd_v, macd_s, macd_h, bull, bear = calc_macd(closes)
    bb_lo, bb_mid, bb_hi             = calc_bbands(closes, 20, 2.0)
    atr                              = calc_atr(highs, lows, closes, 14)
    adx, pdi, mdi                    = calc_adx(highs, lows, closes, 14)
    vwma                             = calc_vwma(closes, volumes, 20)
    atr_pct = (atr / price * 100) if atr and price else None

    return {
        'price':      price,
        'rsi':        round(rsi,  2) if rsi  is not None else None,
        'ma_short':   round(ma_s, 2) if ma_s is not None else None,
        'ma_long':    round(ma_l, 2) if ma_l is not None else None,
        'macd':       round(macd_v, 4) if macd_v is not None else None,
        'macd_sig':   round(macd_s, 4) if macd_s is not None else None,
        'macd_hist':  round(macd_h, 4) if macd_h is not None else None,
        'bull_cross': bull,
        'bear_cross': bear,
        'bb_lo':      round(bb_lo,  2) if bb_lo  is not None else None,
        'bb_mid':     round(bb_mid, 2) if bb_mid is not None else None,
        'bb_hi':      round(bb_hi,  2) if bb_hi  is not None else None,
        'atr':        round(atr,    4) if atr    is not None else None,
        'atr_pct':    round(atr_pct,3) if atr_pct is not None else None,
        'adx':        round(adx, 2) if adx is not None else None,
        'pdi':        round(pdi, 2) if pdi is not None else None,
        'mdi':        round(mdi, 2) if mdi is not None else None,
        'vwma':       round(vwma, 2) if vwma is not None else None,
        'bullish_ma': ma_s is not None and ma_l is not None and ma_s > ma_l,
        'bearish_ma': ma_s is not None and ma_l is not None and ma_s < ma_l,
    }

# ── Market regime ──────────────────────────────────────────────────────────────

def get_regime(ind):
    adx = ind.get('adx')
    if adx is None: return 'UNKNOWN'
    if adx > 25:    return 'TRENDING'
    if adx < 20:    return 'RANGING'
    return 'MIXED'

def is_high_volatility(ind, threshold=3.0):
    return (ind.get('atr_pct') or 0) > threshold

# ── Stop-loss / take-profit check ──────────────────────────────────────────────

def check_stop_tp(state, price, cfg):
    """Returns (signal, reason_str) or None."""
    if not (state.get('coin_held', 0) > 1e-9 and state.get('entry_price')):
        return None
    entry = state['entry_price']
    peak  = state.get('trail_peak', entry)
    pnl   = (price - entry) / entry

    if pnl <= -cfg['stop_loss_pct']:
        return 'SELL', f'STOP-LOSS {pnl*100:.1f}% (entry={entry:.2f})'
    if pnl >= cfg['take_profit_pct']:
        return 'SELL', f'TAKE-PROFIT {pnl*100:.1f}% (entry={entry:.2f})'
    if peak > entry * (1 + cfg['trail_trigger']):
        trail_stop = peak * (1 - cfg['trail_pct'])
        if price < trail_stop:
            return 'SELL', f'TRAIL-STOP peak={peak:.2f} stop={trail_stop:.2f}'
    return None

# ── Signal logic ───────────────────────────────────────────────────────────────
# Requires 2 confirmations to fire — RSI threshold PLUS one of MACD or BB.
# This halves false signals vs single-trigger logic without touching stop/TP.

def get_signal(ind, strategy):
    """Returns (signal, reasons, regime). Stop/TP handled separately."""
    cfg    = STRATEGIES[strategy]
    price  = ind['price']
    rsi    = ind.get('rsi')
    regime = get_regime(ind)

    if rsi is None:
        return 'HOLD', ['warming up'], regime

    macd_h = ind.get('macd_hist') or 0
    bb_lo  = ind.get('bb_lo')
    bb_hi  = ind.get('bb_hi')

    macd_bullish = ind.get('bull_cross') or macd_h > 0
    macd_bearish = ind.get('bear_cross') or macd_h < 0
    at_lower_bb  = bb_lo is not None and price <= bb_lo * 1.005
    at_upper_bb  = bb_hi is not None and price >= bb_hi * 0.995

    signal, reasons = 'HOLD', []

    # BUY: RSI oversold AND (MACD bullish OR price at lower BB)
    if rsi < cfg['rsi_oversold'] and (macd_bullish or at_lower_bb):
        signal = 'BUY'
        confirms = []
        if macd_bullish: confirms.append('MACD bullish')
        if at_lower_bb:  confirms.append(f'lower BB ({bb_lo:.2f})')
        reasons.append(f'RSI={rsi:.1f} + {" & ".join(confirms)} [{regime}]')

    # SELL: RSI overbought AND (MACD bearish OR price at upper BB)
    elif rsi > cfg['rsi_overbought'] and (macd_bearish or at_upper_bb):
        signal = 'SELL'
        confirms = []
        if macd_bearish: confirms.append('MACD bearish')
        if at_upper_bb:  confirms.append(f'upper BB ({bb_hi:.2f})')
        reasons.append(f'RSI={rsi:.1f} + {" & ".join(confirms)} [{regime}]')

    if not reasons:
        reasons = ['no trigger']
    return signal, reasons, regime

# ── Position sizing ────────────────────────────────────────────────────────────

def calc_position_eur(state, price, ind, cfg):
    portfolio = state['balance'] + state['coin_held'] * price
    atr       = ind.get('atr')
    risk_amt  = portfolio * cfg['risk_pct']

    if atr and atr > 0:
        eur_size = (risk_amt / (2.0 * atr)) * price
    else:
        eur_size = portfolio * cfg['max_pos_pct'] * 0.5

    if is_high_volatility(ind):
        eur_size *= 0.5
        log.info('  High volatility — position halved')

    eur_size = min(eur_size, state['balance'] * cfg['max_pos_pct'])
    return max(eur_size, MIN_TRADE_EUR)

# ── State persistence ──────────────────────────────────────────────────────────

def _atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def load_state(strategy, starting_balance):
    for path in (STATE_FILE, STATE_FILE + '.tmp'):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                state = json.load(f)
            if path.endswith('.tmp'):
                log.warning(f'Recovered state from {path}')
            return state
        except Exception as e:
            log.warning(f'{path} unreadable: {e}')

    return {
        'strategy':         strategy,
        'symbol':           '',
        'balance':          starting_balance,
        'coin_held':        0.0,
        'entry_price':      None,
        'trail_peak':       None,
        'checks_done':      0,
        'total_trades':     0,
        'buys':             0,
        'sells':            0,
        'win_trades':       0,
        'loss_trades':      0,
        'total_profit_eur': 0.0,
        'total_loss_eur':   0.0,
        'start_time':       datetime.now(timezone.utc).isoformat(),
        'last_check':       None,
        'last_price':       None,
        'last_rsi':         None,
        'last_signal':      None,
        'last_signal_time': None,
        'peak_value':       starting_balance,
        'trough_value':     starting_balance,
        'starting_balance': starting_balance,
        'daily_start_val':  starting_balance,
        'daily_start_date': datetime.now(timezone.utc).date().isoformat(),
    }

def save_state(state):
    _atomic_write(STATE_FILE, state)

def load_trades():
    for path in (TRADES_FILE, TRADES_FILE + '.tmp'):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []

def append_trade(trade):
    trades = load_trades()
    trades.append(trade)
    _atomic_write(TRADES_FILE, trades)

# ── Trade execution ────────────────────────────────────────────────────────────

def execute_trade(signal, state, price, ind, cfg, check_num, reasons):
    ts  = datetime.now(timezone.utc).isoformat()
    rsn = '; '.join(reasons)

    if signal == 'BUY' and state['balance'] >= MIN_TRADE_EUR and state.get('coin_held', 0) < 1e-9:
        eur_in   = min(calc_position_eur(state, price, ind, cfg), state['balance'])
        coin_out = eur_in / price
        state['balance']    -= eur_in
        state['coin_held']  += coin_out
        state['entry_price'] = price
        state['trail_peak']  = price
        state['total_trades'] += 1
        state['buys']         += 1
        log.info(f'  >> BUY  €{eur_in:>10,.2f} → {coin_out:.6f}  @  €{price:,.2f}  [{rsn}]')
        return {
            'type': 'BUY', 'check': check_num, 'timestamp': ts,
            'price': price, 'eur_spent': round(eur_in, 4),
            'coin_bought': round(coin_out, 8),
            'balance_after': round(state['balance'], 4),
            'coin_after': round(state['coin_held'], 8),
            'reason': rsn, 'atr': ind.get('atr'), 'rsi': ind.get('rsi'),
        }

    if signal == 'SELL' and state.get('coin_held', 0) > 1e-9:
        entry    = state.get('entry_price') or price
        coin_out = state['coin_held']
        eur_in   = coin_out * price
        pnl      = eur_in - coin_out * entry
        state['coin_held']  -= coin_out
        state['balance']    += eur_in
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
        log.info(f'  >> SELL {coin_out:.6f} → €{eur_in:>10,.2f}  @  €{price:,.2f}  pnl={pnl:+.2f}  [{rsn}]')
        return {
            'type': 'SELL', 'check': check_num, 'timestamp': ts,
            'price': price, 'eur_received': round(eur_in, 4),
            'coin_sold': round(coin_out, 8),
            'balance_after': round(state['balance'], 4),
            'coin_after': round(state['coin_held'], 8),
            'pnl': round(pnl, 4), 'entry_price': round(entry, 4),
            'reason': rsn,
        }

    log.info(f'  >> HOLD  [{rsn}]')
    return None

# ── Daily P&L summary ──────────────────────────────────────────────────────────

def maybe_daily_summary(state, price):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get('daily_start_date') == today:
        return
    yesterday_val = state.get('daily_start_val', state['starting_balance'])
    current_val   = state['balance'] + state['coin_held'] * price
    day_pnl       = current_val - yesterday_val
    log.info(f'  ── DAILY P&L: {day_pnl:+.2f} EUR  (€{yesterday_val:,.2f} → €{current_val:,.2f}) ──')
    state['daily_start_date'] = today
    state['daily_start_val']  = current_val

# ── Safety ─────────────────────────────────────────────────────────────────────

def check_kill_switch():
    if os.path.exists(STOP_FILE):
        log.warning('STOP file found — shutting down')
        return True
    return False

def check_pause():
    if os.path.exists(PAUSE_FILE):
        log.warning('PAUSE file found (risk limits hit) — skipping check')
        return True
    return False

def check_health(state):
    last = state.get('last_signal_time')
    if not last:
        return
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
    if age_h > 24:
        log.warning(f'No trade signal in {age_h:.0f}h — possible data issue')

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global STATE_FILE, TRADES_FILE

    if len(sys.argv) < 4:
        print('Usage: python3 bot.py <strategy> <max_checks> <interval> [name] [starting_balance]')
        sys.exit(1)

    arg_strategy     = sys.argv[1].lower()
    max_checks       = int(sys.argv[2])
    interval         = int(sys.argv[3])
    name             = sys.argv[4].upper() if len(sys.argv) > 4 else 'BTC'
    starting_balance = float(sys.argv[5]) if len(sys.argv) > 5 else 1000.0

    if arg_strategy not in STRATEGIES:
        print(f'Unknown strategy. Choose: {", ".join(STRATEGIES)}')
        sys.exit(1)

    STATE_FILE  = f'state_{name}.json'
    TRADES_FILE = f'trades_{name}.json'
    setup_logging(name)

    if name == 'SOL':
        log.warning('SOL trading not recommended — backtest shows -3.6% on flat markets '
                    'even with zero fees. Use BTC or ETH for better results.')

    state    = load_state(arg_strategy, starting_balance)
    strategy = state['strategy']
    cfg      = STRATEGIES[strategy]
    start    = state.get('starting_balance', starting_balance)

    state['max_checks'] = max_checks
    if interval > 0:
        state['interval'] = interval

    # Migrations from v1 state files
    if 'btc_held' in state and 'coin_held' not in state:
        state['coin_held'] = state.pop('btc_held')
    for key, default in [
        ('entry_price', None), ('trail_peak', None),
        ('win_trades', 0), ('loss_trades', 0),
        ('total_profit_eur', 0.0), ('total_loss_eur', 0.0),
        ('last_signal', None), ('last_signal_time', None),
        ('daily_start_val', start),
        ('daily_start_date', datetime.now(timezone.utc).date().isoformat()),
    ]:
        state.setdefault(key, default)

    if not state.get('symbol'):
        state['symbol'] = detect_symbol(name)
        save_state(state)
    symbol = state['symbol']

    bar = '=' * 72
    log.info(bar)
    log.info(f'Crypto Paper Trading Bot v2  [{name}]  strategy={strategy}  {symbol}')
    log.info(f'  {cfg["desc"]}')
    log.info(f'  Budget: €{start:,.0f}  |  Checks: {state["checks_done"]}/{max_checks}')
    log.info(f'  Stop/TP: -{cfg["stop_loss_pct"]*100:.0f}% / +{cfg["take_profit_pct"]*100:.0f}%  '
             f'Trail: +{cfg["trail_trigger"]*100:.0f}% arm → -{cfg["trail_pct"]*100:.0f}% from peak')
    log.info(bar)

    while state['checks_done'] < max_checks:
        if check_kill_switch():
            sys.exit(0)

        check_num = state['checks_done'] + 1
        log.info(f'\n-- [{name}] Check {check_num}/{max_checks}  [{datetime.now().strftime("%H:%M:%S")}] --')

        if check_pause():
            state['checks_done'] += 1
            state['last_check']   = datetime.now(timezone.utc).isoformat()
            save_state(state)
            if interval == 0:
                break
            wake_at = time.time() + interval
            while time.time() < wake_at:
                time.sleep(10)
            continue

        try:
            ohlcv = fetch_ohlcv(symbol, 200)
            ind   = compute_indicators(ohlcv)
            price = ind['price']

            # Update trailing peak
            if state['coin_held'] > 1e-9 and state.get('trail_peak') is not None:
                state['trail_peak'] = max(state['trail_peak'], price)

            # Stop-loss / take-profit overrides indicator signals
            forced = check_stop_tp(state, price, cfg)
            if forced:
                signal, reasons, regime = forced[0], [forced[1]], get_regime(ind)
            else:
                signal, reasons, regime = get_signal(ind, strategy)

            portfolio = state['balance'] + state['coin_held'] * price
            log.info(
                f'  Price: €{price:>10,.2f}  RSI: {str(ind.get("rsi")):>5}  '
                f'MACD_h: {str(ind.get("macd_hist")):>8}  '
                f'ADX: {str(ind.get("adx")):>5}  Regime: {regime}'
            )
            log.info(
                f'  BB: {str(ind.get("bb_lo")):>10} / {str(ind.get("bb_hi")):>10}  '
                f'ATR: {str(ind.get("atr")):>8} ({str(ind.get("atr_pct"))}%)  '
                f'Signal: {signal}'
            )
            log.info(f'  Portfolio: €{portfolio:>10,.2f}  '
                     f'(cash €{state["balance"]:,.2f} + coin €{state["coin_held"]*price:,.2f})')

            maybe_daily_summary(state, price)
            check_health(state)

            trade = execute_trade(signal, state, price, ind, cfg, check_num, reasons)
            if trade:
                append_trade(trade)
                state['last_signal']      = signal
                state['last_signal_time'] = datetime.now(timezone.utc).isoformat()

            portfolio_after       = state['balance'] + state['coin_held'] * price
            state['peak_value']   = max(state['peak_value'],   portfolio_after)
            state['trough_value'] = min(state['trough_value'], portfolio_after)
            state['checks_done'] += 1
            state['last_check']  = datetime.now(timezone.utc).isoformat()
            state['last_price']  = price
            state['last_rsi']    = ind.get('rsi')
            save_state(state)

            pnl     = portfolio_after - start
            wins    = state.get('win_trades', 0)
            losses  = state.get('loss_trades', 0)
            wr      = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
            pf      = state.get('total_profit_eur', 0) / max(state.get('total_loss_eur', 0), 0.01)
            log.info(f'  P&L: {pnl:+,.2f} EUR ({pnl/start*100:+.2f}%)  '
                     f'WinRate: {wr:.0f}%  PF: {pf:.2f}')

        except KeyboardInterrupt:
            log.info('Interrupted. State saved.')
            sys.exit(0)
        except Exception as e:
            log.error(f'  Check failed: {e}')

        if state['checks_done'] < max_checks:
            if interval == 0:
                break
            wake_at = time.time() + interval
            log.info(f'  Sleeping {interval}s …')
            while time.time() < wake_at:
                time.sleep(10)

    price_f = state.get('last_price') or 0
    total   = state['balance'] + state['coin_held'] * price_f
    log.info(f'\n[{name}] DONE  €{total:,.2f}  P&L={total-start:+,.2f}  trades={state["total_trades"]}')

if __name__ == '__main__':
    main()
