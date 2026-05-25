#!/usr/bin/env python3
"""
Terminal dashboard for multi-asset paper trading bot.
Usage: python3 dashboard.py
Refreshes every 5 seconds. Press q or Ctrl+C to exit.
"""

import curses, json, math, os, subprocess, time
from datetime import datetime, timezone

ASSETS   = [('BTC', 'state_BTC.json', 'trades_BTC.json'),
            ('ETH', 'state_ETH.json', 'trades_ETH.json'),
            ('SOL', 'state_SOL.json', 'trades_SOL.json')]
STARTING = {'BTC': 2000, 'ETH': 2000, 'SOL': 1000}

def _load(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def _rel(iso):
    if not iso:
        return '–'
    s = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())
    if s < 60:   return f'{s}s ago'
    if s < 3600: return f'{s//60}m {s%60}s ago'
    return f'{s//3600}h ago'

def _calc_sharpe(equity_points, timeframe='1h'):
    if not equity_points or len(equity_points) < 3:
        return None
    vals = [p['v'] for p in equity_points]
    rets = [(vals[i] - vals[i-1]) / vals[i-1] for i in range(1, len(vals)) if vals[i-1] > 0]
    if len(rets) < 2:
        return None
    mu    = sum(rets) / len(rets)
    sigma = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
    if sigma == 0:
        return None
    ppy = {'1h': 8760, '4h': 2190, '15m': 35040}.get(timeframe, 8760)
    return mu / sigma * math.sqrt(ppy)

def _bot_running(name):
    try:
        out = subprocess.check_output(['pgrep', '-f', f'bot.py.*{name}'], stderr=subprocess.DEVNULL)
        return bool(out.strip())
    except Exception:
        return False

def draw(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_YELLOW,  curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_RED,     curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_CYAN,    curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_MAGENTA, curses.COLOR_BLACK)

    while True:
        key = stdscr.getch()
        if key in (ord('q'), 27):
            break

        stdscr.clear()
        h, w = stdscr.getmaxyx()
        now  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        title = '  Crypto Paper Trading Bot — Dashboard'
        stdscr.addstr(0, 0, title.ljust(w), curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(1, 0, f'  {now}   [q] quit', curses.A_DIM)
        stdscr.addstr(2, 0, '─' * min(w, 80))

        row = 3
        total_val   = 0.0
        total_start = 0.0
        total_wins  = 0
        total_losses = 0

        for name, state_path, trades_path in ASSETS:
            state   = _load(state_path)
            trades  = _load(trades_path) or []
            equity  = _load(f'equity_{name}.json') or []
            start   = STARTING[name]
            running = _bot_running(name)

            if state is None:
                if row < h - 1:
                    stdscr.addstr(row, 2, f'{name}: state file not found', curses.A_DIM)
                row += 2
                continue

            price     = state.get('last_price') or 0
            cash      = state.get('balance', 0)
            held      = state.get('coin_held', 0)
            total     = cash + held * price
            pnl       = total - start
            pnlpct    = pnl / start * 100 if start else 0
            wins      = state.get('win_trades', 0)
            losses    = state.get('loss_trades', 0)
            closed    = wins + losses
            wr        = wins / closed * 100 if closed else None
            profiteur = state.get('total_profit_eur', 0)
            losseur   = state.get('total_loss_eur', 0)
            pf        = profiteur / losseur if losseur > 0 else None
            peak      = state.get('peak_value',   start)
            trough    = state.get('trough_value', start)
            dd        = (peak - total) / peak * 100 if peak > 0 else 0
            sharpe    = _calc_sharpe(equity, state.get('timeframe', '1h'))
            regime    = state.get('last_regime', '?')
            entry     = state.get('entry_price')
            rsi       = state.get('last_rsi')
            tf        = state.get('timeframe', '?')
            dsval     = state.get('daily_start_val', start)
            dpnl      = total - dsval
            strategy  = state.get('strategy', '?')
            checks    = state.get('checks_done', 0)
            max_c     = state.get('max_checks', 4032)
            last_chk  = _rel(state.get('last_check'))

            total_val    += total
            total_start  += start
            total_wins   += wins
            total_losses += losses

            color = {'BTC': 1, 'ETH': 4, 'SOL': 5}.get(name, 1)
            status_color = 2 if running else 3
            status = 'RUNNING' if running else 'stopped'
            header = f'  {name}  [{strategy}]  [{tf}]  {regime}'
            if row < h - 1:
                stdscr.addstr(row, 0, header, curses.color_pair(color) | curses.A_BOLD)
                stdscr.addstr(row, len(header) + 2, f'[{status}]', curses.color_pair(status_color))
            row += 1

            def addrow(label, val_str, attr=0):
                nonlocal row
                if row < h - 2:
                    stdscr.addstr(row, 4, f'{label:<16}', curses.A_DIM)
                    stdscr.addstr(row, 20, val_str, attr)
                row += 1

            addrow('Price',   f'€{price:>12,.2f}')
            addrow('Value',   f'€{total:>12,.2f}')

            sign = '+' if pnl >= 0 else ''
            addrow('P&L', f'{sign}€{abs(pnl):,.2f} ({sign}{pnlpct:.2f}%)',
                   curses.color_pair(2) if pnl >= 0 else curses.color_pair(3))

            dsign = '+' if dpnl >= 0 else ''
            addrow('Daily P&L', f'{dsign}€{abs(dpnl):,.2f}',
                   curses.color_pair(2) if dpnl >= 0 else curses.color_pair(3))

            addrow('Cash',  f'€{cash:>12,.2f}')
            addrow('Held',  f'{held:.6f} {name}')

            if held > 1e-9 and entry:
                ep_pnl = (price - entry) / entry * 100 if price else 0
                esign  = '+' if ep_pnl >= 0 else ''
                addrow('Entry price', f'€{entry:,.2f} ({esign}{ep_pnl:.1f}%)',
                       curses.color_pair(2) if ep_pnl >= 0 else curses.color_pair(3))
            else:
                addrow('Entry price', 'no position', curses.A_DIM)

            rsi_str  = f'{rsi:.1f}' if rsi is not None else '–'
            rsi_attr = (curses.color_pair(2) if rsi and rsi < 35 else
                        curses.color_pair(3) if rsi and rsi > 65 else 0)
            addrow('RSI', rsi_str, rsi_attr)

            addrow('Win rate', f'{wr:.0f}%  ({wins}W/{losses}L)' if wr is not None else '0 trades')

            pf_attr = (curses.color_pair(2) if pf and pf >= 1.5 else
                       curses.color_pair(3) if pf and pf < 1 else 0)
            addrow('Profit factor', f'{pf:.2f}' if pf else '–', pf_attr)

            dd_attr = (curses.color_pair(3) if dd >= 10 else
                       curses.color_pair(1) if dd >= 5  else curses.color_pair(2))
            addrow('Drawdown', f'{dd:.1f}%', dd_attr)
            addrow('Peak',     f'€{peak:,.2f}', curses.color_pair(2))
            addrow('Trough',   f'€{trough:,.2f}', curses.color_pair(3) if trough < start else 0)
            if sharpe is not None:
                sh_attr = (curses.color_pair(2) if sharpe >= 1 else
                           curses.color_pair(3) if sharpe < 0 else curses.color_pair(1))
                addrow('Sharpe', f'{sharpe:.2f}', sh_attr)
            else:
                addrow('Sharpe', '–  (need more data)', curses.A_DIM)
            addrow('Checks',   f'{checks}/{max_c}')
            addrow('Last check', last_chk)

            if trades:
                last_t = trades[-1]
                t_type = last_t.get('type', '?')
                t_rsn  = (last_t.get('reason') or '')[:30]
                addrow('Last trade', f'{t_type} @€{last_t.get("price",0):,.2f}  {t_rsn}',
                       curses.color_pair(2) if t_type == 'BUY' else curses.color_pair(3))

            if row < h - 1:
                stdscr.addstr(row, 0, '─' * min(w, 80), curses.A_DIM)
            row += 1

        # Portfolio summary
        total_pnl    = total_val - total_start
        total_pct    = total_pnl / total_start * 100 if total_start else 0
        total_closed = total_wins + total_losses
        total_wr     = total_wins / total_closed * 100 if total_closed else 0

        if row < h - 2:
            stdscr.addstr(row, 0, '  TOTAL PORTFOLIO', curses.A_BOLD)
            row += 1
            sign = '+' if total_pnl >= 0 else ''
            summary = f'  €{total_val:,.2f}   P&L: {sign}€{abs(total_pnl):,.2f} ({sign}{total_pct:.2f}%)   WR: {total_wr:.0f}%'
            if row < h - 1:
                stdscr.addstr(row, 0, summary,
                              curses.color_pair(2) if total_pnl >= 0 else curses.color_pair(3))
            row += 1

        flags = []
        if os.path.exists('PAUSE'): flags.append('PAUSED')
        if os.path.exists('STOP'):  flags.append('STOPPED')
        if flags and row < h - 1:
            stdscr.addstr(row, 2, '  '.join(flags), curses.color_pair(3) | curses.A_BOLD)

        stdscr.refresh()
        time.sleep(5)

def main():
    try:
        curses.wrapper(draw)
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
