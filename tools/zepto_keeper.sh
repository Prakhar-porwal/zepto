#!/bin/bash
# Keep Zepto's token fresh, hands-off, without launchd/cron.
#
# macOS won't let background schedulers (launchd, cron) execute code inside
# ~/Downloads, but your Terminal can. So start this ONCE from a terminal and
# leave it running -- it tops the token up every 15 minutes, opening Chrome
# off-screen only when the token is actually about to expire.
#
#     nohup ./tools/zepto_keeper.sh >/dev/null 2>&1 &
#
# It survives closing the terminal (nohup), but not a reboot/logout -- after
# those, run the one line again. Stop it with:  pkill -f zepto_keeper
set -uo pipefail

ROOT="/Users/prakharporwal/Downloads/traker"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$ROOT"

echo "[keeper] started $(date '+%H:%M:%S') — refreshing Zepto every 15 min" >> "$ROOT/.zepto-refresh.log"
while true; do
  {
    echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"
    "$ROOT/.venv/bin/python" tools/refresh_zepto_token.py --if-expiring 20
  } >> "$ROOT/.zepto-refresh.log" 2>&1
  sleep 900
done
