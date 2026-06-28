#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$CHROME_PROFILE_DIR" /data/logs
rm -rf "$CHROME_PROFILE_DIR"/slots
rm -f "$CHROME_PROFILE_DIR"/SingletonLock "$CHROME_PROFILE_DIR"/SingletonSocket "$CHROME_PROFILE_DIR"/SingletonCookie

test -c /dev/net/tun || { echo "FATAL: /dev/net/tun is unavailable" >&2; exit 1; }
if [[ "${ISOLATED_VPN_SLOT:-0}" != "1" ]]; then
  test -S /var/run/docker.sock || { echo "FATAL: Docker socket is unavailable" >&2; exit 1; }
fi
for command in Xvfb fluxbox x11vnc websockify python3 chromium openvpn docker; do
  command -v "$command" >/dev/null || { echo "FATAL: required command missing: $command" >&2; exit 1; }
done

Xvfb "$DISPLAY" -screen 0 1365x900x24 -nolisten tcp >/data/logs/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 1

fluxbox >/data/logs/fluxbox.log 2>&1 &
FLUXBOX_PID=$!
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 >/data/logs/x11vnc.log 2>&1 &
X11VNC_PID=$!

websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 >/data/logs/novnc.log 2>&1 &
NOVNC_PID=$!

mkdir -p "${BROWSER_LOG_DIR:-/data/logs}"
python3 /app/browser_manager.py >"${BROWSER_LOG_DIR:-/data/logs}/browser-manager.log" 2>&1 &
MANAGER_PID=$!

deadline=$((SECONDS + 60))
while ! curl -fsS http://127.0.0.1:7070/status >/dev/null 2>&1; do
  kill -0 "$MANAGER_PID" "$XVFB_PID" "$X11VNC_PID" "$NOVNC_PID" || { echo "FATAL: browser service exited during startup" >&2; exit 1; }
  (( SECONDS < deadline )) || { echo "FATAL: browser manager startup timed out" >&2; exit 1; }
  sleep 1
done

tail -F "${BROWSER_LOG_DIR:-/data/logs}/browser-manager.log" /data/logs/novnc.log &
TAIL_PID=$!
while sleep 5; do
  kill -0 "$MANAGER_PID" "$XVFB_PID" "$FLUXBOX_PID" "$X11VNC_PID" "$NOVNC_PID" || { echo "FATAL: browser service stopped" >&2; kill "$TAIL_PID" 2>/dev/null || true; exit 1; }
done
