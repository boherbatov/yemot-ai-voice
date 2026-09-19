#!/bin/sh
node /pot/server/build/main.js &
sleep 2
exec /opt/venv/bin/gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 -k gthread --threads 8 -t 180 --access-logfile - app:app
