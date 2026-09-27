#!/usr/bin/env bash
# Launch the OpenCode compat shim detached so it survives Hermes restarts.
# Re-running is safe: an existing healthy listener on the port is left alone.
set -u
PORT="${1:-18788}"
SRC="C:/Users/User/Documents/Shim_v2/opencode_proxy.py"
LOG="C:/Users/User/AppData/Local/Temp/oc_proxy.log"
ERR="C:/Users/User/AppData/Local/Temp/oc_proxy.err"

# Pick an interpreter that actually has aiohttp. Bare `python` on this box can be
# the Hermes 3.14 tools build, which has no aiohttp -> ModuleNotFoundError.
PY=""
for cand in \
  "C:/Users/User/AppData/Local/Programs/Python/Python314/python.exe" \
  "C:/Users/User/AppData/Local/Programs/Python/Python310/python.exe" \
  "C:/Users/User/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"; do
  if [ -x "$cand" ] && "$cand" -c "import aiohttp" 2>/dev/null; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "FATAL: no python with aiohttp found"; exit 1; fi
echo "interpreter: $PY"

# Already up?
if curl -s -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "already listening on $PORT"
  exit 0
fi

powershell -NoProfile -Command \
  "Start-Process -WindowStyle Hidden '$PY' -ArgumentList '$SRC','--port','$PORT' \
   -RedirectStandardOutput '$LOG' -RedirectStandardError '$ERR'" 2>&1

for i in $(seq 1 20); do
  sleep 1
  if curl -s -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "up after ${i}s"
    curl -s -m 5 "http://127.0.0.1:$PORT/health"
    echo
    exit 0
  fi
done

echo "FAILED to start"
tail -20 "$ERR" 2>/dev/null
exit 1
