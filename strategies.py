#!/usr/bin/env python3
"""
Layer 2: Strategy registry.

Contract
--------
Every strategy is a callable with this exact signature:

    signal_fn(ind: dict, state: dict, check_num: int) -> str

    ind        — output of bot.compute_indicators(window)
    state      — mutable simulation state (entry_price, trail_peak,
                 last_exit_check, balance, coin_held, …)
    check_num  — bar index within the fold simulation (used for cooldown)
    returns    — 'BUY' | 'SELL' | 'HOLD'

Stop-loss / take-profit is handled by the walk-forward engine (via
bot.check_stop_tp) *before* calling the signal function.  Strategies
only generate entry/exit intent; hard exits override them.

Registry
--------
REGISTRY maps strategy names to (signal_fn, config_dict) pairs.
The config_dict must be compatible with bot.check_stop_tp and
bot.calc_position_eur (keys: stop_loss_pct, take_profit_pct,
trail_trigger, trail_pct, risk_pct, max_pos_pct).

Adding a new strategy
---------------------
1. Implement a signal function with the contract above.
2. Add an entry to REGISTRY: {'my_strategy': (my_fn, my_cfg)}.
   If the strategy lives in bot.STRATEGIES, reference it there.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import get_signal, STRATEGIES


# ── signal functions ──────────────────────────────────────────────────────────
# Thin wrappers so bot.get_signal is the single source of truth for logic.
# funding_rate=None — no live funding data in research simulations.

def _aggressive(ind, state, check_num):
    signal, _, _ = get_signal(ind, 'aggressive', state, check_num, None)
    return signal

def _trend_follow(ind, state, check_num):
    signal, _, _ = get_signal(ind, 'trend_follow', state, check_num, None)
    return signal


# ── registry ──────────────────────────────────────────────────────────────────

REGISTRY = {
    'aggressive':   (_aggressive,   STRATEGIES['aggressive']),
    'trend_follow': (_trend_follow, STRATEGIES['trend_follow']),
}
