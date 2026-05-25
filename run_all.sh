#!/usr/bin/env bash
# Start paper trading bots for BTC and ETH in the background.
# SOL is skipped by default (poor backtest performance).
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

echo "Starting bots with timeframe=$TF ..."

# BTC  aggressive  2000 EUR  1h checks  run forever (0 interval = single-check mode handled by GH Actions)
nohup python3 bot.py aggressive 999999 3600 BTC 2000 --timeframe "$TF" \
  > /dev/null 2>&1 &
echo $! > pid_BTC.pid
echo "  BTC started (PID $(cat pid_BTC.pid))"

# ETH  aggressive  2000 EUR
nohup python3 bot.py aggressive 999999 3600 ETH 2000 --timeframe "$TF" \
  > /dev/null 2>&1 &
echo $! > pid_ETH.pid
echo "  ETH started (PID $(cat pid_ETH.pid))"

echo "Logs: bot_BTC.log  bot_ETH.log"
echo "Stop: ./stop_all.sh"
