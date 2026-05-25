#!/usr/bin/env python3
"""
Portfolio-level risk manager.
Run after each bot check: python3 risk_manager.py
Creates PAUSE file if daily loss > 5% or drawdown > 15%.
Removes PAUSE file when limits are no longer breached.
"""

import json, os, glob
from datetime import datetime, timezone

PAUSE_FILE        = 'PAUSE'
DAILY_LOSS_LIMIT  = 0.05   # pause if down 5% in current day
MAX_DRAWDOWN_LIMIT = 0.15  # pause if down 15% from all-time portfolio peak

def _load_states():
    states = []
    for path in sorted(glob.glob('state_*.json')):
        try:
            with open(path, encoding='utf-8') as f:
                states.append((path, json.load(f)))
        except Exception as e:
            print(f'  Warning: could not read {path}: {e}')
    return states

def main():
    states = _load_states()
    if not states:
        print('No state_*.json files found.')
        return

    total_val        = 0.0
    total_peak       = 0.0
    total_daily_start = 0.0
    today            = datetime.now(timezone.utc).date().isoformat()

    print(f'\n{"─"*54}')
    print(f'  Risk Manager  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'{"─"*54}')

    for path, s in states:
        price    = s.get('last_price') or 0
        val      = s.get('balance', 0) + s.get('coin_held', 0) * price
        peak     = s.get('peak_value', val)
        ds_val   = s.get('daily_start_val', s.get('starting_balance', val))
        name     = path.replace('state_', '').replace('.json', '')
        total_val         += val
        total_peak        += peak
        total_daily_start += ds_val
        print(f'  {name:<4}  €{val:>9,.2f}  peak €{peak:>9,.2f}  daily_start €{ds_val:>9,.2f}')

    drawdown   = (total_peak - total_val) / total_peak if total_peak > 0 else 0.0
    daily_loss = (total_daily_start - total_val) / total_daily_start if total_daily_start > 0 else 0.0

    print(f'{"─"*54}')
    print(f'  TOTAL  €{total_val:>9,.2f}  peak €{total_peak:>9,.2f}')
    print(f'  Drawdown from peak : {drawdown*100:>5.1f}%  (limit {MAX_DRAWDOWN_LIMIT*100:.0f}%)')
    print(f'  Daily loss         : {daily_loss*100:>5.1f}%  (limit {DAILY_LOSS_LIMIT*100:.0f}%)')
    print(f'{"─"*54}')

    reasons = []
    if drawdown >= MAX_DRAWDOWN_LIMIT:
        reasons.append(f'drawdown {drawdown*100:.1f}% >= {MAX_DRAWDOWN_LIMIT*100:.0f}%')
    if daily_loss >= DAILY_LOSS_LIMIT:
        reasons.append(f'daily loss {daily_loss*100:.1f}% >= {DAILY_LOSS_LIMIT*100:.0f}%')

    # Correlation guard: if BTC is held, flag it (informational)
    held_assets = [p.replace('state_','').replace('.json','')
                   for p, s in states if s.get('coin_held', 0) > 1e-9]
    if len(held_assets) > 1:
        print(f'  Correlated positions held: {", ".join(held_assets)}')
        print(f'  Note: sizing already halved per ATR logic when multiple positions open.')

    if reasons:
        msg = 'PAUSED — ' + '; '.join(reasons)
        with open(PAUSE_FILE, 'w', encoding='utf-8') as f:
            f.write(f'{msg}\nTimestamp: {datetime.now(timezone.utc).isoformat()}\n')
        print(f'  !! {msg}')
        print(f'  Created {PAUSE_FILE}')
    elif os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
        print(f'  OK  Removed {PAUSE_FILE} — all limits within range')
    else:
        print(f'  OK  All risk limits within range')

if __name__ == '__main__':
    main()
