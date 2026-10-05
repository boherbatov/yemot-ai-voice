#!/bin/sh
set -eu
export MALLOC_ARENA_MAX=2   # fewer glibc arenas: noticeably less RSS in threaded python and node
POT_HOME="${POT_HOME:-/app/pot}"
if [ ! -f "$POT_HOME/server/build/main.js" ]; then POT_HOME=/pot; fi
NODE="$POT_HOME/server/node_modules/node/bin/node"
if [ ! -x "$NODE" ]; then NODE=node; fi
"$NODE" "$POT_HOME/server/build/main.js" &
POT_PID=$!
trap 'kill "$POT_PID" 2>/dev/null || true' EXIT TERM INT
/opt/venv/bin/python - <<'PYTEST'
import time, urllib.request
for i in range(5):
    try:
        urllib.request.urlopen('http://127.0.0.1:4416/ping', timeout=1)
        break
    except Exception:
        time.sleep(1)
PYTEST
/opt/venv/bin/gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 -k gthread --threads 8 -t 180 --access-logfile - --access-logformat '%(h)s %(t)s "%(m)s %(U)s" %(s)s %(b)s %(L)s' app:app
