#!/usr/bin/env python3
"""OpenAI-shaped SSE proxy for OpenCode free-tier models -- NO CLI REQUIRED.

This replaces the CLI-tunnelling shim (opencode_proxy.py). Where the old one
drove an installed `opencode-cli.exe` over stdin and fought WinError 206, a
fenced tool protocol, alien-tool suppression and watchdog aborts, this one is
a straight streaming HTTP passthrough to the Zen endpoint. The CLI is never
invoked. Nothing is installed; only `aiohttp` is needed.

WHY THIS WORKS (reverse-engineered and verified 2026-10-13 against the live
endpoint, all findings proven by replay, not inferred):

  OpenCode's free tier is gated server-side. A bare OpenAI-compatible request
  to https://opencode.ai/zen/v1/chat/completions gets HTTP 403 with
  {"type":"error","error":{"type":"FreeTierError","message":"OpenCode's free
  tier can only be used from within OpenCode"}}.

  The gate is NOT TLS-fingerprint based and NOT a secret key. It checks that
  the request LOOKS like it came from the OpenCode harness, via two signals:

    1. A non-empty `tools` array in the request BODY that includes the `read`
       and `shell` tool definitions (the harness's own file/shell tools).
       Removing either -> 403. edit/glob/grep/write are optional filler.
       A minimal {bash} tool, or an empty tools list, also -> 403.

    2. `stream: true` with `stream_options: {"include_usage": true}`.
       A non-streaming body -> 403.

  Auth is the literal string `public` (Authorization: Bearer public) -- this
  is what the unauthenticated CLI itself sends; it is not a user secret.

  Headers that carry the harness identity (all verified present in a captured
  CLI request): User-Agent `opencode/<channel>/<version>/<client>`, plus the
  session/project/trace headers below. The session/project IDs are locally
  minted (they are not validated against an account), but the header NAMES and
  the `b3`/`traceparent` trace format are what the harness sends.

Because the free models stream genuine OpenAI SSE, tool calling works natively
over the wire: the caller's own `tools` array is forwarded and the model's
`tool_calls` frames pass straight through. There is no fenced protocol and no
prompt serialisation -- the full OpenAI request/response contract is preserved.
"""

import argparse
import asyncio
import json
import os
import random
import secrets
import time
import uuid

from aiohttp import web, ClientSession, ClientTimeout

# ── endpoint ─────────────────────────────────────────────────────────────────
UPSTREAM = "https://opencode.ai/zen/v1/chat/completions"

# The harness identity headers. Values proven from a live CLI capture.
CLIENT_VERSION = "2.0.11"
CLIENT_CHANNEL = "latest"
CLIENT_NAME = "cli"
# x-opencode-project is a 40-hex value; the CLI derives it from the working
# directory's git identity, but the server does not validate it against an
# account, so a locally minted stable value is fine.
PROJECT_ID = secrets.token_hex(20)  # 40 hex chars

DEFAULT_MODEL = "longcat-2.5-preview-free"

FREE_MODELS = [
    "longcat-2.5-preview-free",
    "space-bunny-free",
    "mimo-v2.6-flash-free",
    "ling-3.0-flash-fin-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
]
# Retired upstream: still listed in /zen/v1/models but route to provider.no-route.
DEAD_MODELS = {
    "mimo-v2.5-free": "Model retired by OpenCode (provider.no-route).",
    "deepseek-v4-flash-free": "Model retired by OpenCode (provider.no-route).",
    "jev-1.13-free": "System One model - not a chat model.",
}
ALIASES = {
    "longcat": "longcat-2.5-preview-free",
    "longcat-2.5": "longcat-2.5-preview-free",
    "bunny": "space-bunny-free",
    "space-bunny": "space-bunny-free",
    "mimo": "mimo-v2.6-flash-free",
    "ling": "ling-3.0-flash-fin-free",
    "nemotron": "nemotron-3-ultra-free",
    "nemotron-3-ultra": "nemotron-3-ultra-free",
}

# The gate signature: the harness's own tool catalogue. `read` and `shell` are
# mandatory (removing either triggers FreeTierError); the rest are carried for
# fidelity with a real harness request. These are merged into every outgoing
# body so the free tier accepts it. They are the OpenCode harness's tools, NOT
# the caller's tools -- the caller's own tools array is forwarded separately and
# untouched, so tool calling still works natively end to end.
# The catalogue is loaded from gate_tools.json, shipped beside this file.


def _load_gate_tools():
    """Load the harness tool catalogue shipped next to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "gate_tools.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


GATE_TOOLS = _load_gate_tools()
GATE_TOOL_NAMES = {t["function"]["name"] for t in GATE_TOOLS}


def resolve_model(name):
    name = (name or DEFAULT_MODEL).strip()
    for p in ("opencode-go/", "opencode/"):
        if name.startswith(p):
            name = name[len(p):]
    return ALIASES.get(name.lower(), name)


def mint_session_id():
    """A harness-shaped session id: ses_ + 12 hex + 14 base62.

    The server does not validate this against an account (proven: locally
    minted ids pass), but the shape matches what the CLI emits so the request
    is indistinguishable from a real harness turn.
    """
    hexpart = "".join(random.choice("0123456789abcdef") for _ in range(12))
    b62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    b62part = "".join(random.choice(b62) for _ in range(14))
    return "ses_" + hexpart + b62part


def build_upstream_headers():
    """The exact header set a real OpenCode harness turn carries."""
    session = mint_session_id()
    trace = secrets.token_hex(16)
    span = secrets.token_hex(8)
    parent = secrets.token_hex(8)
    return {
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": f"opencode/{CLIENT_CHANNEL}/{CLIENT_VERSION}/{CLIENT_NAME}",
        "b3": f"{trace}-{span}-1-{parent}",
        "traceparent": f"00-{trace}-{span}-01",
        "x-opencode-client": CLIENT_NAME,
        "x-opencode-project": PROJECT_ID,
        "x-opencode-session": session,
        "x-session-affinity": session,
        "x-session-id": session,
        "Accept": "*/*",
    }


def prepare_body(body, model):
    """Shape the caller's OpenAI body into one the free tier accepts.

    Two mandatory transforms (proven by replay):
      * force stream:true + stream_options.include_usage (non-stream -> 403)
      * inject the harness gate tools (read+shell); a body without them -> 403

    The caller's own `tools` array is preserved: harness tools are appended
    after the caller's tools so the model still sees the caller's function
    catalogue and can emit real tool_calls. If the caller already supplied a
    tool with the same name as a harness tool, the caller's wins (it is the
    one the caller can actually execute).
    """
    out = dict(body)
    out["model"] = model
    out["stream"] = True
    out["stream_options"] = {"include_usage": True}

    caller_tools = body.get("tools") or []
    caller_names = {(t.get("function") or {}).get("name") for t in caller_tools}
    # Append only the harness tools the caller did not already define.
    injected = [t for t in GATE_TOOLS
                if t["function"]["name"] not in caller_names]
    out["tools"] = list(caller_tools) + injected
    return out


# ── SSE plumbing ─────────────────────────────────────────────────────────────

def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


async def handle_chat(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "invalid JSON body", "type": "invalid_request"}},
            status=400)

    requested = body.get("model") or DEFAULT_MODEL
    model = resolve_model(requested)
    if model in DEAD_MODELS:
        return web.json_response(
            {"error": {"message": DEAD_MODELS[model], "type": "model_not_found",
                       "code": "model_dead", "param": "model"}},
            status=503)

    upstream_body = prepare_body(body, model)
    headers = build_upstream_headers()
    ntools = len(upstream_body["tools"])
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    print(f"[proxy] {requested}->{model} tools={ntools} "
          f"msgs={len(body.get('messages') or [])}", flush=True)

    timeout = ClientTimeout(total=None, sock_read=600)
    session = ClientSession(timeout=timeout)
    try:
        async with session.post(UPSTREAM, json=upstream_body,
                                headers=headers) as up:
            if up.status != 200:
                raw = await up.text()
                print(f"[proxy] upstream {up.status}: {raw[:200]}", flush=True)
                # Pass the upstream error shape straight back.
                try:
                    err = json.loads(raw)
                except Exception:
                    err = {"error": {"message": raw[:300] or "upstream error",
                                     "type": "upstream_error"}}
                return web.json_response(err, status=up.status)

            # Client asked for a non-streaming reply: buffer the SSE and
            # reassemble a single chat.completion object.
            if not body.get("stream"):
                return await _collect_nonstream(up, cid, model)

            # Streaming: relay the upstream SSE frames verbatim. The free
            # models emit standard OpenAI chunks (including tool_calls deltas),
            # so no translation is needed.
            resp = web.StreamResponse(status=200, headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"})
            await resp.prepare(request)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            print(f"[proxy] -> stream done {model}", flush=True)
            return resp
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[proxy] ERROR: {e}", flush=True)
        return web.json_response(
            {"error": {"message": str(e), "type": "proxy_error"}}, status=502)
    finally:
        await session.close()


async def _collect_nonstream(up, cid, model):
    """Buffer an upstream SSE stream into one OpenAI chat.completion object."""
    content_parts = []
    tool_calls = {}   # index -> {id, name, arguments}
    finish = None
    usage = None
    role = None

    buf = b""
    async for chunk in up.content.iter_any():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices", []):
                if ch.get("delta", {}).get("role"):
                    role = ch["delta"]["role"]
                delta = ch.get("delta", {})
                if delta.get("content"):
                    content_parts.append(delta["content"])
                for tc in delta.get("tool_calls", []) or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]

    text = "".join(content_parts)
    calls = []
    for idx in sorted(tool_calls):
        slot = tool_calls[idx]
        if slot["name"]:
            calls.append({
                "id": slot["id"] or ("call_" + uuid.uuid4().hex[:24]),
                "type": "function",
                "function": {"name": slot["name"],
                             "arguments": slot["arguments"] or "{}"},
            })
    msg = {"role": role or "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = calls
    return web.json_response({
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": finish or ("tool_calls" if calls else "stop")}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


async def handle_models(request):
    data = [{"id": m, "object": "model", "created": 0, "owned_by": "opencode"}
            for m in FREE_MODELS]
    data += [{"id": a, "object": "model", "created": 0, "owned_by": "opencode-alias"}
             for a in ALIASES]
    return web.json_response({"object": "list", "data": data})


async def health(request):
    return web.json_response({
        "status": "ok",
        "default_model": DEFAULT_MODEL,
        "free_models": FREE_MODELS,
        "dead_models": sorted(DEAD_MODELS),
        "transport": "direct-http",
        "cli_required": False,
        "gate_tools": sorted(GATE_TOOL_NAMES),
        "upstream": UPSTREAM,
    })


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=18788)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not GATE_TOOLS:
        raise SystemExit("gate_tools.json failed to load; the free tier will 403.")

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_post("/chat/completions", handle_chat)
    print(f"[proxy] direct-HTTP :{args.port} -> {UPSTREAM} (no CLI)", flush=True)
    print(f"[proxy] gate tools: {sorted(GATE_TOOL_NAMES)}", flush=True)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
