# OpenCode Free-Tier Shim v2 — Technical Analysis

## 1. Problem Statement

OpenCode (opencode.ai) exposes a "free-tier" model roster (big-pickle,
muse-spark-1.3-contributor-free, mimo-v2.5-free, deepseek-v4-flash-free).
Their public API is locked behind desktop-app identity — bare HTTP
requests without the session cookies injected by the Electron client
return **403 Forbidden**. The gate is not cryptographic: OpenCode ships a
signed CLI binary (`opencode-cli.exe`) that uses the desktop session's
credentials (stored as browser cookies / local auth tokens under the
user's `AppData\Roaming\ai.opencode.desktop\` tree) to talk to the
provider endpoint. There is no API key, no token endpoint, no
machine-authenticable flow. The only way to call the free tier is
through that CLI.

Any external client that wants free-tier models must therefore funnel
its requests through the CLI — which speaks **neither** the OpenAI wire
protocol **nor** SSE. It takes a single prompt on argv and emits a
newline-delimited JSON event stream on stdout (events like
`{"type":"text","part":{"text":"..."}}`). It is a per-call tool, not a
server: each invocation is a brand-new process with zero memory of
previous calls.

Hermes Agent, like almost every AI-agent framework, talks to its
providers over an OpenAI-shaped SSE protocol
(`POST /v1/chat/completions`, `stream: true`, `data: ...` frames,
`data: [DONE]` terminator). It also sends the **full conversation
history** on every turn and expects the model to remember prior
exchanges.

The incompatibility, in one line: **Hermes speaks streaming OpenAI; the
CLI speaks buffered per-call JSON**. A naive adapter — collect the
whole CLI response, return one JSON blob — produces exactly the
failure Hermes reports as *"Provider returned an empty stream with no
finish_reason"* because Hermes was waiting for SSE frames that never
came.

## 2. Why v1 Failed

The first shim (v1) worked as a stateless translator:

```
Hermes  -->  proxy  -->  opencode run --format json  -->  proxy  -->  Hermes
         (buffer everything,                      (single JSON reply)
          strip to text)
```

v1 had three fatal defects:

### 2.1 No streaming
v1 buffered the entire CLI response into one string and returned a
non-streaming `chat.completion` JSON object. Hermes, expecting SSE,
saw no chunk boundaries, no `finish_reason` in a `delta`, and
eventually timed out with the exact error above. v1 also died every
time the Hermes session turn ended (it was launched in Hermes's
foreground process group and got reaped when that group went away),
so it had to be restarted by hand every ~15 minutes.

### 2.2 Conversation amnesia
v1 took only the **last user message** out of the `messages` array it
received. Every turn, Hermes re-sends the entire conversation
(`system` + every prior `user`/`assistant`/`tool` turn), and the
model needs that history to give coherent replies. v1 threw all of
it away — the model answered each prompt in isolation.

### 2.3 Single-path routing
v1 only registered `/v1/chat/completions`. The OpenAI Python client
(and therefore Hermes, which uses it) POSTs to `/chat/completions`
(no `/v1/` prefix) by default. v1 returned 404 on that path, which
Hermes reported as a connection error.

## 3. What v2 Changes

### 3.1 True SSE streaming
v2 registers an `aiohttp.StreamResponse` and writes `data: ...\n\n`
frames as the CLI emits them. The wire format now matches the
OpenAI SSE contract:

```
data: {"id":"...","choices":[{"delta":{"role":"assistant","content":""}}]}

data: {"id":"...","choices":[{"delta":{"content":"The"}}]}

data: {"id":"...","choices":[{"delta":{"content":" answer"}}]}

data: {"id":"...","choices":[{"finish_reason":"stop"}]}

data: [DONE]
```

The proxy sends an initial role chunk immediately so Hermes receives
HTTP headers without waiting for the CLI's first token. It then
streams text deltas, a `finish_reason: stop` frame, and a clean
`[DONE]` terminator. A non-streaming path is preserved for any
client that does not set `stream: true`.

### 3.2 Full conversation flattening
v2 adds `flatten_history(messages)`, which converts the full
`messages` array into a single labeled transcript:

```
[system]
You are Hermes Agent, an intelligent AI assistant...

[user]
What is my favorite color?

[assistant]
You told me it's teal.

[user]
What is my favorite color?
```

Because the CLI has no cross-call memory, this transcript is the
only way to carry context into the fresh process. Multi-turn memory
was verified end-to-end: the model answered "teal" to the second
question after being given only the transcript.

### 3.3 Both API paths
v2 registers both `/v1/chat/completions` and `/chat/completions`,
so Hermes's default client works without overriding the base URL's
trailing segment.

### 3.4 Detached process launch
v2 is launched via PowerShell `Start-Process -WindowStyle Hidden`,
redirecting stdout/stderr to a log file. It is not a child of the
Hermes session process, so it survives session turns, desktop-app
restarts, and does not block the Hermes event loop. It must be
started before Hermes, but only once per boot.

## 4. OpenCode Security Posture — What We Accounted For

OpenCode's "free tier" is not an open API. The security model is
**transport identity via signed desktop client**, not authentication.
Observations:

- **No API key**: there is nothing to put in `Authorization: Bearer`.
  The CLI authenticates implicitly through the desktop session's
  local state (cookies / credential blob under the user's AppData
  tree). The shim never sees or handles a secret.

- **Session binding**: the CLI refuses to run if the desktop app's
  session state is wiped or uninitialized. This is why wiping
  `%APPDATA%\ai.opencode.desktop` breaks the free tier — the shim
  depends on that state being intact.

- **User-agent and header fingerprinting**: earlier experiments
  (not part of this repo) showed that bare `urllib`/`requests`
  traffic without the desktop-managed cookies gets rejected with
  403 even if the request shape is otherwise correct. The CLI
  bypasses this by loading the browser cookie jar.

- **Per-process statelessness**: each `opencode run` spawns a fresh
  OS process. There is no session ID, no thread identity, no
  connection pooling across calls. The shim reflects this — every
  request is independent CLI exec. This has cost (process spawn +
  model cold-start latency on every turn) but it is what the
  platform enforces.

- **No rate-limit visibility**: the CLI does not surface 429s in a
  structured way; under load it simply hangs or errors. The shim
  passes errors back as HTTP 502 with the stderr tail.

None of this is adversarial to the shim — we are using the CLI
exactly as intended (called from a user session on the user's
machine). We are not cracking, spoofing, or reverse-engineering
anything. The shim is equivalent to a shell script that calls the
CLI on stdin.

## 5. Remaining Friction With Hermes

The shim works end-to-end for raw HTTP. For it to be a first-class
Hermes provider, three things still need wiring inside Hermes
itself:

1. **Provider registration** — Hermes's `config.yaml` has no
   `opencode-free` entry. The provider must be added so it appears
   in `/model` and survives restarts. Minimal addition:

   ```yaml
   model:
     default: big-pickle
     provider: custom
     base_url: http://127.0.0.1:18788/v1
     api_key: none  # shim ignores auth
   ```

   Or, as a model alias under an existing custom provider, so it
   does not disturb the current default.

2. **Streaming provider feature flag** — Hermes must know the
   provider supports `stream: true`. If the provider entry lacks
   this, Hermes may downgrade to non-stream and lose the latency
   benefit.

3. **Proxy auto-start** — the shim is a long-running local server.
   Hermes does not currently manage background adapters. Two
   options: (a) a logon-scheduled task that runs the shim hidden
   on boot, or (b) a tiny Hermes plugin / wrapper script that
   checks `curl /health` and starts the proxy lazily before the
   first request. Either way, it must be `SIGTERM`-clean so it
   does not leave a zombie on the port.

Once (1)-(3) land, `/model big-pickle` should just work, and the
free tier becomes a first-class citizen alongside the OAuth and
key-based providers.

## 6. Files

| File | Purpose |
|------|---------|
| `opencode_proxy.py` | The shim itself — aiohttp server, ~190 LOC |
| `joke.png` | Documentation artifact (see §1) |
| `README.md` | How to run, what still needs doing |

## 7. Usage

```powershell
# one-time per boot, from PowerShell
Start-Process -WindowStyle Hidden python -ArgumentList `
  'C:\Users\User\Documents\Shim_v2\opencode_proxy.py','--port','18788' `
  -RedirectStandardOutput "$env:TEMP\oc_proxy.log" `
  -RedirectStandardError "$env:TEMP\oc_proxy.err"

# test
curl http://127.0.0.1:18788/health
# {"status":"ok"}

curl -X POST http://127.0.0.1:18788/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"big-pickle\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```
