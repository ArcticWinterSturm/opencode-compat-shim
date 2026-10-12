# shim_v3 - start script (PowerShell)
# OpenCode free-tier proxy (CLI-free, direct HTTP). Only dependency: aiohttp.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python opencode_direct_proxy.py --port 18788
