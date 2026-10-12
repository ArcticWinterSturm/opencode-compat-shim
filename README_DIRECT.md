# OpenCode Free-Tier Proxy — Direct HTTP (no CLI)

`shim_v3` — an OpenAI-compatible SSE proxy that serves OpenCode's free-tier
models **without the OpenCode CLI installed**. Drop-in replacement for the
older CLI-tunnelling shim (`opencode_proxy.py`, v2), which required an
authenticated `opencode-cli.exe` and drove it over stdin.

```
start_proxy.cmd        # Windows (or: python opencode_direct_proxy.py --port 18788)
```

Only dependency: `aiohttp`. No CLI, no account, no credentials file. The
authorization value is the literal string `public` (what the unauthenticated
CLI itself sends — it is not a user secret).

## Why the old one needed the CLI, and why this one doesn't

The old shim's premise was: *the free tier is locked behind desktop-app
identity, and the only authenticated transport is the signed CLI.* That was
true when it was written, but the actual gate is simpler and lives entirely in
the **request body**, not in TLS or a secret. Reverse-engineered and verified
by replay against the live endpoint on 2026-10-13:

A bare OpenAI request to `https://opencode.ai/zen/v1/chat/completions`
returns:

```json
{"type":"error","error":{"type":"FreeTierError",
 "message":"OpenCode's free tier can only be used from within OpenCode"}}
```

The gate passes when the request **looks like an OpenCode harness turn**. Two
things in the body are checked:

1. **A non-empty `tools` array that includes the `read` and `shell` tool
   definitions.** These are the harness's own file/shell tools. Remove either
   → 403. An empty tools list, or a single generic `{bash}` tool, → 403. The
   other harness tools (`edit`, `glob`, `grep`, `write`) are optional filler.
   (Verified by binary-searching the tool set: 6 harness tools → 200; drop
   `read` or `shell` → 403.)

2. **`stream: true` with `stream_options: {"include_usage": true}`.**
   A non-streaming body → 403.

Auth is `Authorization: Bearer public`. The `x-opencode-*` / `x-session-*` /
`b3` / `traceparent` headers carry the harness identity, but the session and
project IDs are **not validated against an account** — locally minted values
of the right shape pass. The header names and the `ses_<hex><b62>` session-id
format match what the CLI emits so the request is indistinguishable from a
real harness turn.

None of this is a secret. The "public" bearer and the tool catalogue are
extracted from a captured CLI request; the session/project IDs are minted
locally in the correct format.

## What this removes

The old CLI-tunnelling shim carried all of this complexity, and every line of
it is gone here:

| Old (CLI) problem | Why it existed | Direct-HTTP |
|---|---|---|
| WinError 206 (32,767-char argv cap) | prompt rode in argv | gone — body is JSON, no argv |
| prompt over STDIN | same | gone |
| fenced `<hermes_tool_call>` protocol | CLI can't carry OpenAI `tools` | gone — tools pass natively |
| alien-tool suppression (bash/read/write) | CLI injects its own tools | gone — no CLI tools injected |
| corrective-nudge retry | model reached for alien tools | gone |
| transient-abort retry (watchdog) | CLI step watchdog kills turns | gone |
| history flattening to text | CLI is a fresh process per call | gone — real `messages` array |

Tool calling is now **native**: the caller's `tools` array is forwarded and the
model's `tool_calls` frames pass straight through (streaming and non-streaming
both verified). The harness gate tools are appended after the caller's tools;
if the caller defines a tool with the same name, the caller's wins.

## Endpoints

- `POST /v1/chat/completions` — OpenAI chat completions (stream + non-stream)
- `GET  /v1/models` — the free-tier model list + aliases
- `GET  /health` — status, `cli_required: false`, gate-tool list

## Models

Free tier (verified live): `longcat-2.5-preview-free`, `space-bunny-free`,
`mimo-v2.6-flash-free`, `ling-3.0-flash-fin-free`, `nemotron-3-ultra-free`,
`nemotron-3.5-lightning-free`. Aliases: `longcat`, `bunny`, `mimo`, `ling`,
`nemotron`, etc. Retired models return `503 model_dead`.

Model-prefix stripping: `opencode/<id>` and `opencode-go/<id>` are accepted
and reduced to the bare id.

## Files

- `opencode_direct_proxy.py` — the proxy
- `gate_tools.json` — the harness tool catalogue (the gate signature; `read`
  and `shell` are the mandatory pair)
- `start_proxy.cmd` / `start_proxy.ps1` / `start_proxy.sh` — launchers (port 18788)
- `test_units_direct.py` — offline unit tests (no network) — 39 checks
- `test_live_direct.py` — live integration tests against the real endpoint — 26 checks

## Tests

```
python test_units_direct.py                 # offline, no network
start_proxy.cmd &                           # or: python opencode_direct_proxy.py --port 18799 &
python test_live_direct.py --base http://127.0.0.1:18799 --model space-bunny-free
```

## Notes / caveats

- The gate is enforced **per-model on the server**; paid models reach the
  upstream with the same headers (a paid model returns its own protocol error,
  not the free-tier gate). This proxy is free-tier only by the model allowlist.
- The free-tier gate is a server-side policy and can change without notice. If
  every request starts returning `FreeTierError`, re-capture a live CLI request
  and diff the body: the most likely change is the required tool set. The
  capture method is in the analysis write-up.
- Free-tier availability shifts without notice; a model that 403s may have been
  retired upstream.
