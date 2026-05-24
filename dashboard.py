#!/usr/bin/env python3
"""
BTC Bot Live Dashboard
Run: python3 dashboard.py
Press Q to quit.
"""

import curses
import json
import os
import time
from datetime import datetime

STATE_FILE  = 'state.json'
TRADES_FILE = 'trades.json'
PID_FILE    = 'bot.pid'
STARTING    = 1000.0
REFRESH     = 5

def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def bot_alive():
    import subprocess
    try:
        out = subprocess.check_output(['pgrep', '-f', 'bot.py'], text=True).strip()
        if out:
            pid = int(out.splitlines()[0])
            return True, pid
    except Exception:
        pass
    return False, None

def safe_add(win, row, col, text, attr=0, max_w=None):
    h, w = win.getmaxyx()
    if row >= h - 1 or col >= w:
        return
    if max_w:
        text = text[:max_w]
    text = text[:w - col - 1]
    try:
        win.addstr(row, col, text, attr)
    except curses.error:
        pass

def draw_dashboard(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN,  -1)
    curses.init_pair(2, curses.COLOR_RED,    -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN,   -1)
    curses.init_pair(5, curses.COLOR_WHITE,  -1)

    GREEN  = curses.color_pair(1) | curses.A_BOLD
    RED    = curses.color_pair(2) | curses.A_BOLD
    YELLOW = curses.color_pair(3) | curses.A_BOLD
    CYAN   = curses.color_pair(4) | curses.A_BOLD
    BOLD   = curses.A_BOLD
    DIM    = curses.A_DIM
    REV    = curses.A_REVERSE

    stdscr.nodelay(True)
    stdscr.timeout(500)

    last_refresh = 0

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q')):
            break

        now = time.time()
        if now - last_refresh < REFRESH:
            continue
        last_refresh = now

        stdscr.erase()
        h, w = stdscr.getmaxyx()

        state  = read_json(STATE_FILE)
        trades = read_json(TRADES_FILE) or []
        alive, pid = bot_alive()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # ── Title bar ──────────────────────────────────────────────────────────
        title = '  BTC PAPER TRADING DASHBOARD  '
        safe_add(stdscr, 0, max(0, (w - len(title)) // 2), title, BOLD | REV)

        row = 2

        if not state:
            safe_add(stdscr, row, 2, 'No state.json found — bot has not run yet.', RED)
            safe_add(stdscr, h - 1, 0, ' Q quit ', REV)
            stdscr.refresh()
            continue

        strategy = state.get('strategy', '?').upper()
        symbol   = state.get('symbol', 'BTCEUR')

        # ── Status row ────────────────────────────────────────────────────────
        if alive:
            safe_add(stdscr, row, 2, '● LIVE', GREEN)
            safe_add(stdscr, row, 10, f'PID {pid}', DIM)
        else:
            safe_add(stdscr, row, 2, '○ STOPPED', RED)

        safe_add(stdscr, row, 22, f'Strategy: {strategy}', CYAN)
        safe_add(stdscr, row, 42, f'Symbol: {symbol}', DIM)
        safe_add(stdscr, row, w - len(ts) - 2, ts, DIM)
        row += 1

        # ── Progress bar ──────────────────────────────────────────────────────
        done  = state.get('checks_done', 0)
        total = state.get('max_checks', 4032)
        pct   = done / total * 100
        bar_w = min(40, w - 30)
        filled = int(bar_w * done / total)
        bar = '█' * filled + '░' * (bar_w - filled)
        safe_add(stdscr, row, 2, f'Progress  [{bar}]  {done}/{total}  ({pct:.1f}%)', DIM)
        row += 2

        # ── Portfolio box ─────────────────────────────────────────────────────
        price     = state.get('last_price') or 0
        cash      = state.get('balance', 0)
        btc       = state.get('btc_held', 0)
        btc_val   = btc * price
        total_val = cash + btc_val
        pnl       = total_val - STARTING
        pnl_pct   = pnl / STARTING * 100
        peak      = state.get('peak_value', STARTING)
        trough    = state.get('trough_value', STARTING)

        safe_add(stdscr, row, 2, '┌─ PORTFOLIO ──────────────────────────────────────────┐', DIM)
        row += 1

        safe_add(stdscr, row, 2, '│', DIM)
        safe_add(stdscr, row, 4, 'Total Value :')
        safe_add(stdscr, row, 18, f'€{total_val:>11,.2f}', BOLD)
        row += 1

        pnl_color = GREEN if pnl >= 0 else RED
        safe_add(stdscr, row, 2, '│', DIM)
        safe_add(stdscr, row, 4, 'P&L         :')
        safe_add(stdscr, row, 18, f'{pnl:>+11,.2f} EUR  ({pnl_pct:>+.2f}%)', pnl_color)
        row += 1

        safe_add(stdscr, row, 2, '│', DIM)
        safe_add(stdscr, row, 4, 'Cash        :')
        safe_add(stdscr, row, 18, f'€{cash:>11,.2f}', DIM)
        row += 1

        safe_add(stdscr, row, 2, '│', DIM)
        safe_add(stdscr, row, 4, 'BTC held    :')
        safe_add(stdscr, row, 18, f'{btc:.8f}  =  €{btc_val:,.2f}', DIM)
        row += 1

        safe_add(stdscr, row, 2, '│', DIM)
        safe_add(stdscr, row, 4, 'BTC Price   :')
        safe_add(stdscr, row, 18, f'€{price:>11,.2f}', YELLOW)
        row += 1

        safe_add(stdscr, row, 2, '│', DIM)
        safe_add(stdscr, row, 4, f'Peak  €{peak:,.2f}    Trough  €{trough:,.2f}', DIM)
        row += 1

        safe_add(stdscr, row, 2, '└──────────────────────────────────────────────────────┘', DIM)
        row += 2

        # ── Trade stats ───────────────────────────────────────────────────────
        n_total = state.get('total_trades', 0)
        n_buys  = state.get('buys', 0)
        n_sells = state.get('sells', 0)
        safe_add(stdscr, row, 2, f'Trades: {n_total} total  —  ', DIM)
        safe_add(stdscr, row, 20, f'{n_buys} buys', GREEN)
        safe_add(stdscr, row, 27, '  /  ', DIM)
        safe_add(stdscr, row, 32, f'{n_sells} sells', RED)
        row += 2

        # ── Recent trades table ───────────────────────────────────────────────
        if trades:
            hdr = '  #     TYPE    PRICE (EUR)    BTC AMOUNT        EUR VALUE    TIME (UTC)'
            safe_add(stdscr, row, 2, '┌─ RECENT TRADES' + '─' * max(0, len(hdr) - 14) + '┐', DIM)
            row += 1
            safe_add(stdscr, row, 2, '│' + hdr + '│', DIM)
            row += 1
            safe_add(stdscr, row, 2, '│' + '─' * len(hdr) + '│', DIM)
            row += 1

            shown = trades[-10:][::-1]
            for t in shown:
                if row >= h - 3:
                    break
                t_type  = t.get('type', '?')
                t_color = GREEN if t_type == 'BUY' else RED
                t_price = t.get('price', 0)
                t_btc   = t.get('btc_bought') or t.get('btc_sold', 0)
                t_eur   = t.get('eur_spent') or t.get('eur_received', 0)
                t_time  = t.get('timestamp', '')[:16].replace('T', ' ')
                t_check = t.get('check', 0)

                line = f'  {t_check:<5}  '
                safe_add(stdscr, row, 2, '│' + line, DIM)
                col = 2 + 1 + len(line)
                safe_add(stdscr, row, col, f'{t_type:<6}', t_color)
                rest = f'  €{t_price:>10,.2f}    {t_btc:.8f}    €{t_eur:>10,.2f}    {t_time}'
                safe_add(stdscr, row, col + 6, rest)
                row += 1

            safe_add(stdscr, row, 2, '└' + '─' * len(hdr) + '┘', DIM)
            row += 1

        # ── Footer ────────────────────────────────────────────────────────────
        footer = f'  Q quit  |  auto-refresh every {REFRESH}s  |  last update: {ts}  '
        safe_add(stdscr, h - 1, 0, footer.ljust(w - 1), REV)

        stdscr.refresh()

def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    curses.wrapper(draw_dashboard)

if __name__ == '__main__':
    main()
