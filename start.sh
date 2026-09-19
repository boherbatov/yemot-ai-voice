#!/bin/sh
node /pot/server/build/main.js &
sleep 2
exec /opt/venv/bin/gunicorn -b 0.0.0.0:${PORT:-10000} -t 300 -w 2 app:app
