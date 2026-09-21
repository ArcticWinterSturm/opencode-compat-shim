@echo off
REM Start opencode serve and the OpenAI-compatible proxy for Hermes Agent
REM Run this before starting Hermes to enable free-tier OpenCode models

setlocal EnableDelayedExpansion

set "OPENCODE_CLI=C:\Users\User\AppData\Roaming\ai.opencode.desktop\cli\2.0.11\opencode-cli.exe"
set "PROXY_PORT=18788"
set "OPENCODE_PORT=18787"
set "AUTH_USER=opencode"

echo [start] Checking if opencode serve is already on port %OPENCODE_PORT%...
netstat -ano | findstr ":%OPENCODE_PORT%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [start] opencode serve already running on port %OPENCODE_PORT%
    goto :start_proxy
)

echo [start] Starting opencode serve on port %OPENCODE_PORT%...
start /B "%OPENCODE_CLI%" serve --port %OPENCODE_PORT% --print-logs > "%TEMP%\opencode-serve.log" 2>&1
timeout /t 3 /nobreak >nul

echo [start] Reading server password from log...
for /f "tokens=*" %%a in ('findstr "server password" "%TEMP%\opencode-serve.log%"') do set "PWD_LINE=%%a"
for /f "tokens=3" %%a in ("%PWD_LINE%") do set "AUTH_PASS=%%a"
echo [start] Password: %AUTH_PASS%

:start_proxy
echo [start] Starting OpenAI-compatible proxy on port %PROXY_PORT%...
cd /d C:\Users\User\Documents\opencode-proxy
start /B python opencode_proxy.py --host 127.0.0.1 --port %PROXY_PORT% --server http://127.0.0.1:%OPENCODE_PORT% --auth %AUTH_USER%:%AUTH_PASS%

echo [start] Proxy ready at http://127.0.0.1:%PROXY_PORT%
echo [start] Test: curl http://127.0.0.1:%PROXY_PORT%/health
