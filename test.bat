@echo off
REM Start proxy, test it, then stop — for verification

cd /c/Users\User

echo Starting proxy...
start /B python Documents/opencode-proxy\opencode_proxy.py --port 18788 > proxy_test.log 2>&1

timeout /t 3 /nobreak >nul

echo Testing...
curl -s http://127.0.0.1:18788/health
echo.
curl -s -X POST http://127.0.0.1:18788/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"muse-spark-1.3-contributor-free\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"say hello\"}]}" 2>nul
echo.

echo Log:
type proxy_test.log

echo Cleaning up...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *proxy*" 2>nul
taskkill /F /IM python.exe /FI "PID eq *" 2>nul
