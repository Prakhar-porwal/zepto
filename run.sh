#!/bin/bash
# Start traker (server + public ngrok tunnel) and print the live URL.
# Idempotent: skips whatever is already running.
set -uo pipefail
ROOT="/Users/prakharporwal/Downloads/traker"
cd "$ROOT"

# 1. API server on :8000
if lsof -ti :8000 >/dev/null 2>&1; then
  echo "server: already running on :8000"
else
  echo "server: starting…"
  nohup "$ROOT/.venv/bin/uvicorn" server:app --host 127.0.0.1 --port 8000 >"$ROOT/.server.log" 2>&1 &
  for i in $(seq 1 20); do curl -s -o /dev/null http://127.0.0.1:8000/ && break; sleep 0.5; done
  echo "server: up"
fi

# 2. public tunnel
if curl -s http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
  echo "ngrok: already running"
else
  echo "ngrok: starting…"
  nohup ngrok http 8000 --log=stdout >"$ROOT/.ngrok.log" 2>&1 &
  sleep 3
fi

# 3. print the public URL
for i in $(seq 1 15); do
  URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | "$ROOT/.venv/bin/python" -c "import sys,json
try: print(next(x['public_url'] for x in json.load(sys.stdin)['tunnels'] if x['public_url'].startswith('https')))
except Exception: pass" 2>/dev/null)
  [ -n "${URL:-}" ] && break
  sleep 1
done
echo ""
echo "  ►  LIVE at:  ${URL:-<tunnel not ready, check .ngrok.log>}"
echo "     (first visit shows an ngrok 'Visit Site' button — click through once)"
