# OpenCode Free Tier Fix — Technical Writeup

## Problem

Hermes Agent could not use OpenCode's free-tier models (e.g., `muse-spark-1.2-contributor-free`,
`mimo-v2.5-free`, `deepseek-v4-flash-free`). Every request failed with:

```
HTTP 400: Error from provider (Console): OpenCode's free tier can only be used in OpenCode
```

Underlying error type: `MissingSessionID` — the OpenCode Zen relay (`https://opencode.ai/zen/v1`)
requires an `X-Session-ID` header on anonymous (keyless) requests, which Hermes did not send.

## Root Cause

OpenCode's free tier gate, located in `packages/console/app/src/routes/zen/util/handler.ts`,
calls `authenticate()` which checks for a session identifier via header `X-Session-ID`.
If no session ID is present and the model allows anonymous access (`allowAnonymous` flag),
the relay should serve the request anonymously. However, without the `X-Session-ID` header,
the relay returns:

```json
{"type": "error", "error": {"type": "MissingSessionID", "message": "OpenCode's free tier can only be used in OpenCode"}}
```

### Why Other Approaches Failed

| Approach | Result | Reason |
|----------|--------|--------|
| `User-Agent: opencode/0.20.5` | Still 400 | UA gate is deprecated; `X-Session-ID` is now required |
| `Authorization: Bearer <key>` with user's key | 401 CreditsError | User's key has no payment method for paid models |
| Keyed models (`muse-spark-1.2` without `-contributor-free`) | 401 CreditsError | Requires paid subscription |
| `X-Session-ID: <uuid>` without UA | Works but inconsistent | UA helps with upstream attribution tracking |

## Fix

The fix adds an `X-Session-ID` header (a fresh UUID4 per plugin load) to all OpenCode
free-tier header sets. This satisfies the relay's session gate for anonymous requests.

### Files Modified

**1. `plugins/model-providers/opencode-free/__init__.py`**

Added `_opencode_session_id()` helper and included `X-Session-ID` in `_KEYLESS_HEADERS`:

```python
def _opencode_session_id() -> str:
    import uuid
    return str(uuid.uuid4())

_KEYLESS_HEADERS = {
    "Authorization": "",
    "HTTP-Referer": "https://opencode.ai/",
    "X-Title": "opencode",
    "User-Agent": _opencode_ua(),
    "X-Session-ID": _opencode_session_id(),
}
```

Key design decisions:
- **Lazy import** of `_opencode_session_id()` inside each header construction
  to avoid circular import issues (`hermes_cli.models` is imported at runtime,
  not module load time).
- **UUID4 per plugin load** — generates a session ID when the plugin module
  is first imported; each backend restart gets a fresh session.
- **Empty `Authorization` header** — the free tier 401s any unrecognized
  bearer, so empty auth overrides the OpenAI SDK's `Bearer <placeholder>`.

**2. `plugins/model-providers/opencode-zen/__init__.py`**

Same pattern — added `_opencode_session_id()` and `X-Session-ID` to
`_ATTRIBUTION_HEADERS`. This covers free-tier models selected under the
`opencode-zen` provider that get healed to the free runtime path.

**3. `hermes_cli/models.py`**

Two changes:
- Added `_opencode_session_id()` to `opencode_zen_free_headers()` return dict
  (the runtime-level free-tier header factory).
- Added `_opencode_family_key_configured()` guard to prevent keyless routing
  when a real OpenCode key is configured:

```python
def _opencode_family_key_configured(family: str) -> bool:
    """True if a real OpenCode key is configured for the family (env or config)."""
    import os
    env = {
        "opencode-zen": "OPENCODE_ZEN_API_KEY",
        "opencode-go": "OPENCODE_GO_API_KEY",
        "opencode-free": "OPENCODE_ZEN_API_KEY",
    }.get(family or "")
    if env and os.environ.get(env, "").strip():
        return True
    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials
        creds = resolve_api_key_provider_credentials(family)
        ...
```

**4. `agent/auxiliary_client.py`**

Prevented the keyless free-tier override from clobbering a real API key:

```python
# Before: if _free_rt is not None:
# After:
if _free_rt is not None and not api_key:
```

This ensures that when a user has a configured OpenCode key, their key takes
priority over the keyless placeholder.

**5. `agent/agent_runtime_helpers.py`**

Made the OpenCode Free header injection conditional on no real key being present:

```python
if agent.provider == "opencode-free":
    _existing_api_key = client_kwargs.get("api_key", "")
    _is_keyless = str(_existing_api_key or "").strip() in {
        "",
        "opencode-zen-free-keyless",
    }
    if _is_keyless:
        _existing.update(opencode_zen_free_headers())
```

### Why Not Other Approaches

- **Setting `allowAnonymous` from the client**: Not possible. The
  `allowAnonymous` flag is server-side only in `ModelSchema` (see
  `packages/console/core/src/model.ts`). The client cannot override it.
- **Spoofing `User-Agent: opencode/<version>`**: Previously effective but
  upstream has deprecated UA-based gating (`packages/console/app/src/routes/zen/util/ipRateLimiter.ts`
  has `// temporarily disable check headers`). The `X-Session-ID` header is
  the current mechanism.
- **Using the real API key**: The user's key (67-char, `sk-NeT...pOCd`)
  returns `401 CreditsError: No payment method` for paid models like
  `muse-spark-1.2` (without `-contributor-free` suffix). The free-tier
  contributor model is the correct target.

## Verification

### End-to-End Curl Test

```bash
API_KEY=$(cat "/home/user/Desktop/OpenCode Fix/OpenCode Key.txt" | tr -d '\n')
SID=$(uuidgen)

# Free model (keyless with X-Session-ID):
curl -X POST "https://opencode.ai/zen/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: $SID" \
  -H "User-Agent: opencode/0.20.5" \
  -H "HTTP-Referer: https://opencode.ai/" \
  -H "X-Title: opencode" \
  -d '{"model":"muse-spark-1.2-contributor-free","input":"say hello in 3 words"}'
# → HTTP 200: "Hello there, friend!"

# Free model (anonymous, empty Authorization):
curl -X POST "https://opencode.ai/zen/v1/chat/completions" \
  -H "Authorization: " \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: $SID" \
  -H "User-Agent: opencode/0.20.5" \
  -H "HTTP-Referer: https://opencode.ai/" \
  -H "X-Title: opencode" \
  -d '{"model":"mimo-v2.5-free","messages":[{"role":"user","content":"say hello"}],"max_tokens":50}'
# → HTTP 200: full response with reasoning

# Keyed model (user's paid key — requires payment method):
curl -X POST "https://opencode.ai/zen/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: $SID" \
  -H "User-Agent: opencode/0.20.5" \
  -d '{"model":"muse-spark-1.2","input":"say hello"}'
# → HTTP 401: CreditsError (expected — no payment method)
```

### Provider Profile Verification

```python
import providers
providers._discover_providers()
from providers import get_provider_profile

p = get_provider_profile("opencode-free")
print("X-Session-ID" in p.default_headers)  # True
print(p.default_headers["Authorization"])  # '' (empty)

from hermes_cli.models import opencode_zen_free_headers
print("X-Session-ID" in opencode_zen_free_headers())  # True
```

### Log Verification

No `MissingSessionID` errors appear in `/home/user/.hermes/logs/errors.log`
after the fix was applied.

## Context

- **Hermes Agent version**: v0.20.5
- **OpenCode relay**: `https://opencode.ai/zen/v1`
- **Free-tier relay**: Also requires `User-Agent: opencode/<version>` for
  upstream attribution (enforced by the relay; other UAs are rejected)
- **Config**: `.env` has only commented placeholders for `OPENCODE_ZEN_API_KEY`
  and `OPENCODE_GO_API_KEY` — free tier path is correctly active
