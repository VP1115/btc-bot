#!/usr/bin/env bash
# Stop all running paper trading bots gracefully via STOP file,
# then kill any remaining bot.py processes.

set -euo pipefail
cd "$(dirname "$0")"

echo "Creating STOP file ..."
touch STOP

for pid_file in pid_BTC.pid pid_ETH.pid pid_SOL.pid; do
  if [[ -f "$pid_file" ]]; then
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
      echo "  Killing PID $pid ($pid_file) ..."
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  fi
done

# Catch any stragglers
remaining=$(pgrep -f "python3 bot.py" 2>/dev/null || true)
if [[ -n "$remaining" ]]; then
  echo "  Killing remaining bot.py processes: $remaining"
  echo "$remaining" | xargs kill 2>/dev/null || true
fi

echo "All bots stopped. Remove STOP when ready to restart."
