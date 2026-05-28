#!/usr/bin/env bash
# Start paper trading bot for ETH only.
# BTC disabled 2026-05 — mean reversion unprofitable across adx 20/22 + RSI 32/35 (best PF 0.92). Re-enable only with a BTC-specific strategy or different regime.
# SOL disabled 2026-05 — Sharpe 0.30, fails risk-adjusted threshold. Re-enable only with SOL-specific tuning.
# Usage: ./run_all.sh [--timeframe 1h|4h|15m]

set -euo pipefail
cd "$(dirname "$0")"

TF="${2:-1h}"
for arg in "$@"; do
  case "$arg" in
    --timeframe=*) TF="${arg#*=}" ;;
    --timeframe)   shift; TF="${1:-1h}" ;;
  esac
done

if [[ -f STOP ]]; then
  echo "STOP file exists — remove it first: rm STOP"
  exit 1
fi

echo "Starting ETH bot with timeframe=$TF ..."

nohup python3 bot.py aggressive 999999 3600 ETH 2000 --timeframe "$TF" \
  > /dev/null 2>&1 &
echo $! > pid_ETH.pid
echo "  ETH started (PID $(cat pid_ETH.pid))"

echo "Log: bot_ETH.log"
echo "Stop: ./stop_all.sh"
