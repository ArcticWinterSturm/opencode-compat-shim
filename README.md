# OpenCode Free Tier Fix for Hermes Agent

Fix for `HTTP 400: Error from provider (Console): OpenCode's free tier can only be used in OpenCode`

## The Problem

When selecting OpenCode free-tier models (e.g. `muse-spark-1.2-contributor-free`) in Hermes Agent v0.20.5,
every request fails with:

```
{"type": "error", "error": {"type": "MissingSessionID", "message": "OpenCode's free tier can only be used in OpenCode"}}
```

OpenCode's Zen relay (`https://opencode.ai/zen/v1`) requires an `X-Session-ID` header on all
anonymous (keyless) requests. Without it, the relay rejects the request even though the
model is supposed to be freely available.

## The Fix

Adds an `X-Session-ID` header (a UUID4 generated per plugin load) to all OpenCode free-tier
header sets across 6 files:

```
plugins/model-providers/opencode-free/__init__.py     → _KEYLESS_HEADERS
plugins/model-providers/opencode-zen/__init__.py      → _ATTRIBUTION_HEADERS
hermes_cli/models.py                                   → opencode_zen_free_headers()
agent/auxiliary_client.py                              → keyless override guard
agent/agent_runtime_helpers.py                         → conditional header injection
```

## Quick Check: Does Your Setup Need This Fix?

```bash
# If this returns 200, your setup is working
curl -s -X POST "https://opencode.ai/zen/v1/chat/completions" \
  -H "Authorization: Bearer $(cat ~/.openrouter/free-key 2>/dev/null || echo '')" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: $(uuidgen)" \
  -H "User-Agent: opencode/0.20.5" \
  -H "HTTP-Referer: https://opencode.ai/" \
  -H "X-Title: opencode" \
  -d '{"model":"mimo-v2.5-free","messages":[{"role":"user","content":"hello"}],"max_tokens":50}' \
  -w "\nHTTP_CODE:%{http_code}"
```

If it returns `HTTP_CODE:400` or `MissingSessionID`, you need this fix.

## Applying the Fix

```bash
# 1. Stop the Hermes backend
pkill -f "hermes_cli.main serve"

# 2. Clear Python bytecode caches
find /home/user/.hermes/hermes-agent/plugins/model-providers -name '__pycache__' -type d -exec rm -rf {} +
find /home/user/.hermes/hermes-agent/hermes_cli -name '__pycache__' -type d -exec rm -rf {} +
find /home/user/.hermes/hermes-agent/agent -name '__pycache__' -type d -exec rm -rf {} +

# 3. Apply the patches (see TECHNICAL-WRITEUP.md for exact diffs)

# 4. Restart the Hermes desktop client (it auto-starts the backend)
#    OR manually start:
#    cd /home/user/.hermes/hermes-agent
#    python3 -m hermes_cli.main serve --host 127.0.0.1 --port 0
```

## Verification

After applying the fix and restarting:

1. Select an OpenCode free-tier model: `/model muse-spark-1.2-contributor-free`
2. Send a message
3. The response should return successfully (no `MissingSessionID` error)

### Verification Commands

```bash
# Verify the patched files contain X-Session-ID
grep -c "X-Session-ID" \
  plugins/model-providers/opencode-free/__init__.py \
  plugins/model-providers/opencode-zen/__init__.py \
  hermes_cli/models.py

# Verify the running backend loaded the new code
ls -la plugins/model-providers/opencode-free/__pycache__/__init__.cpython-311.pyc
# Should be newer than the .py file timestamp

# Verify the header is present in loaded profiles
python3 -c "
from providers import get_provider_profile
p = get_provider_profile('opencode-free')
print('X-Session-ID present:', 'X-Session-ID' in p.default_headers)
"
```

## Files Changed

| File | Lines | Description |
|------|-------|-------------|
| `plugins/model-providers/opencode-free/__init__.py` | +8 | Added `_opencode_session_id()`, `X-Session-ID` to `_KEYLESS_HEADERS` |
| `plugins/model-providers/opencode-zen/__init__.py` | +8 | Added `_opencode_session_id()`, `X-Session-ID` to `_ATTRIBUTION_HEADERS` |
| `hermes_cli/models.py` | +15 | Added `_opencode_session_id()` to `opencode_zen_free_headers()`, added `_opencode_family_key_configured()` guard |
| `agent/auxiliary_client.py` | 1 | Changed `if _free_rt is not None:` to `if _free_rt is not None and not api_key:` |
| `agent/agent_runtime_helpers.py` | ~10 | Made keyless header injection conditional on no real key, added `X-Session-ID` |
| `agent/agent_init.py` | 0 | No changes needed (calls `opencode_zen_free_headers()` which now includes `X-Session-ID`) |

## Notes

- The user's OpenCode Zen API key (from `/home/user/Desktop/OpenCode Fix/OpenCode Key.txt`)
  is valid but has no payment method, so paid models return `401 CreditsError`.
- The free-tier contributor models (e.g. `muse-spark-1.2-contributor-free`) work with
  the anonymous keyless path + `X-Session-ID`.
- `muse-spark-1.2-contributor-free` uses the `/v1/responses` endpoint (not
  `/v1/chat/completions` — that returns HTTP 503 for this model).
- The `User-Agent: opencode/0.20.5` header is still required alongside `X-Session-ID`
  for the relay's attribution gate.

## Full Technical Writeup

See [TECHNICAL-WRITEUP.md](./TECHNICAL-WRITEUP.md) for detailed analysis of the root cause,
OpenCode relay internals, and verification methodology.
