#!/bin/bash
# Cron/launchd wrapper: keep Zepto's token fresh from the logged-in Chrome
# profile. cron runs with a bare PATH and no working directory, so both are set
# explicitly here. Only actually opens Chrome when the token is about to lapse.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Homebrew (node) + system paths; cron's default PATH omits /opt/homebrew/bin
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

cd "$ROOT"
exec >>"$ROOT/.zepto-refresh.log" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"
"$ROOT/.venv/bin/python" tools/refresh_zepto_token.py --if-expiring 20
