#!/usr/bin/env bash
# shim_v3 - start script (bash/Git-Bash/WSL)
# OpenCode free-tier proxy (CLI-free, direct HTTP). Only dependency: aiohttp.
cd "$(dirname "$0")"
exec python opencode_direct_proxy.py --port 18788
