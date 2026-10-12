@echo off
REM shim_v3 - OpenCode free-tier proxy (CLI-free, direct HTTP)
REM Starts the proxy on port 18788. Only dependency: aiohttp.
cd /d "%~dp0"
python opencode_direct_proxy.py --port 18788
