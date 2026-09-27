@echo off
REM Launch the OpenCode compat shim detached at logon.
REM start_proxy.ps1 picks a python that actually has aiohttp, is idempotent
REM (no-op if the port already answers), and waits for /health before exiting.
REM Registry autostart entry: HKCU\...\Run\OpenCodeCompatShim
REM start "" /b detaches, so logon never blocks on the PowerShell child.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
  -File "C:\Users\User\Documents\Shim_v2\start_proxy.ps1" 18788 ^
  >> "C:\Users\User\AppData\Local\Temp\oc_proxy.log" 2>> "C:\Users\User\AppData\Local\Temp\oc_proxy.err"
exit /b 0
