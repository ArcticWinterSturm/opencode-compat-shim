@echo off
REM Launch the OpenCode compat shim detached at logon.
REM start_proxy.ps1 picks a python that has aiohttp, is idempotent
REM (no-op if the port already answers), and waits for /health before exiting.
REM Registry autostart entry: HKCU\...\Run\OpenCodeCompatShim
REM
# Do NOT redirect into oc_proxy.log here: start_proxy.ps1 already hands that
# file to the proxy process. Two writers on one file makes Windows refuse the
# open ("cannot access the file because it is being used by another process")
# and the proxy then never starts.
REM
REM There is deliberately no ">>" on this line either: cmd.exe opens a redirect
# target exclusively, so two starters racing at logon collide and print an
# error. start_proxy.ps1 appends its own diagnostics via Add-Content (open,
REM write, close), which is safe to run concurrently. The proxy's stdout/stderr
REM are handed to it by Start-Process inside that script.
REM start "" /b detaches, so logon never blocks on the PowerShell child.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
  -File "C:\Users\User\Documents\Shim_v2\start_proxy.ps1" 18788
exit /b 0
