#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$CHROME_PROFILE_DIR" /data/logs
rm -rf "$CHROME_PROFILE_DIR"/slots
rm -f "$CHROME_PROFILE_DIR"/SingletonLock "$CHROME_PROFILE_DIR"/SingletonSocket "$CHROME_PROFILE_DIR"/SingletonCookie

Xvfb "$DISPLAY" -screen 0 1365x900x24 -nolisten tcp >/data/logs/xvfb.log 2>&1 &
sleep 1

fluxbox >/data/logs/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 >/data/logs/x11vnc.log 2>&1 &

websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 >/data/logs/novnc.log 2>&1 &

mkdir -p "${BROWSER_LOG_DIR:-/data/logs}"
python3 /app/browser_manager.py >"${BROWSER_LOG_DIR:-/data/logs}/browser-manager.log" 2>&1 &

while ! nc -z 127.0.0.1 7070; do
  sleep 1
done

tail -F "${BROWSER_LOG_DIR:-/data/logs}/browser-manager.log" /data/logs/novnc.log
