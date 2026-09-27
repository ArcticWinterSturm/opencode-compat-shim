#!/usr/bin/env python3
"""OpenAI-shaped SSE-streaming proxy that tunnels through the OpenCode CLI.

OpenCode's free-tier API is locked behind desktop-app identity — bare HTTP
requests are rejected (403). The only authenticated transport is their own
CLI binary, which carries the desktop session. This proxy is therefore a
translation shim: OpenAI-shaped requests in (as Hermes Agent sends them)
-> CLI invocation (authenticated) -> OpenAI-shaped SSE stream out.

The CLI spawns a fresh, stateless process per call, so conversation history
must ride in the prompt. The proxy flattens the full message list into a
single labeled transcript that the model can read.
"""

import asyncio, json, os, uuid, argparse
from aiohttp import web

OPENCODE_CLI = os.path.expandvars(
    r"%APPDATA%\ai.opencode.desktop\cli\2.0.11\opencode-cli.exe"
)
WORK_DIR = os.path.expandvars(r"%USERPROFILE%\Documents")
PER_READ_TIMEOUT = 180  # seconds between opencode output lines

DEFAULT_MODEL = "longcat-2.5-preview-free"

# Free-tier models, verified live against the CLI on 2026-09-28.
# Excluded by request: big-pickle, muse-spark-*-contributor-free (Meta).
# Advertised by OpenCode but DEAD (`provider.no-route`) — kept in DEAD_MODELS
# so callers get a clear message instead of an opaque 502:
#   mimo-v2.5-free, deepseek-v4-flash-free, jev-1.13-free
FREE_MODELS = [
    "longcat-2.5-preview-free",     # 1M ctx, multimodal, zero data retention
    "space-bunny-free",             # stealth, zero-retention provider
    "mimo-v2.6-flash-free",
    "ling-3.0-flash-fin-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
]

DEAD_MODELS = {
    "mimo-v2.5-free": "Model retired by OpenCode (provider.no-route).",
    "deepseek-v4-flash-free": "Model retired by OpenCode (provider.no-route).",
    "jev-1.13-free": "System One model - not a chat model, unreachable via CLI.",
}

# Short aliases so `/model longcat` etc. resolve.
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


def resolve_model(name):
    """Map a requested model id onto a known-live free model id."""
    name = (name or DEFAULT_MODEL).strip()
    if name.startswith("opencode/"):
        name = name[len("opencode/"):]
    if name.startswith("opencode-go/"):
        name = name[len("opencode-go/"):]
    return ALIASES.get(name.lower(), name)


def cli_env():
    """Subprocess env with the MSYS PWD var fixed.

    opencode-cli.exe chdir()s to $PWD. Under Git-Bash/MSYS PWD is a POSIX
    path ('/c/Users/...') that the Windows binary cannot chdir into, and every
    call dies with 'Failed to change directory to /c/Users/...'. Pointing PWD
    at a native path makes the binary chdir cleanly.
    """
    env = dict(os.environ)
    env["PWD"] = WORK_DIR
    return env


async def run_opencode(model, text):
    """Yield text deltas as opencode emits them on stdout."""
    cmd = [OPENCODE_CLI, "run", "--format", "json", "--model", f"opencode/{model}", text]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=WORK_DIR, env=cli_env(),
    )
    sent = ""

    async def pump():
        nonlocal sent
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=PER_READ_TIMEOUT)
            if not line:
                break
            line = line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "error":
                err = obj.get("error", {})
                raise RuntimeError(
                    f"{err.get('type', 'error')}: {err.get('message', 'unknown error')}"
                )
            if obj.get("type") == "text":
                part = obj.get("part", {})
                new = part.get("text", obj.get("text", ""))
                if new and len(new) > len(sent):
                    yield new[len(sent):]
                    sent = new
        rc = await proc.wait()
        if rc != 0:
            err = (await proc.stderr.read()).decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"opencode exit {rc}: {err}")

    try:
        async for delta in pump():
            yield delta
    except Exception:
        if proc.returncode is None:
            await proc.kill()
        raise


def chunk(completion_id, model, delta=None, finish=None):
    d = {}
    if delta is not None:
        d["delta"] = delta if isinstance(delta, dict) else {"content": delta}
    if finish:
        d["finish_reason"] = finish
    return json.dumps({
        "id": completion_id, "object": "chat.completion.chunk",
        "created": 0, "model": model,
        "choices": [{"index": 0, **d}],
    })


def flatten_history(msgs):
    """Flatten full Hermes conversation into one prompt for `opencode run`.

    The CLI spawns a fresh session per call, so context must ride in the
    prompt itself. Roles are labeled; tool results are included so the
    model can react to them. Multimodal parts are flattened to text.
    """
    lines = []
    for m in msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            continue
        if role == "system":
            lines.append(f"[system]\n{content}")
        elif role == "assistant":
            lines.append(f"[assistant]\n{content}")
        elif role == "tool":
            lines.append(f"[tool result]\n{content}")
        else:
            lines.append(f"[user]\n{content}")
    return "\n\n".join(lines).strip()


async def handle_chat(request):
    body = await request.json()
    requested = body.get("model") or DEFAULT_MODEL
    model = resolve_model(requested)
    stream = body.get("stream", False)
    msgs = body.get("messages", [])
    text = flatten_history(msgs) or "Hello"

    if model in DEAD_MODELS:
        return web.json_response(
            {"error": {"message": DEAD_MODELS[model], "type": "model_not_found",
                       "code": "model_dead", "param": "model"}},
            status=503,
        )

    print(f"[proxy] {'stream' if stream else 'block '} {requested}->{model}: {text[:60]!r}",
          flush=True)
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if not stream:
        parts = []
        try:
            async for delta in run_opencode(model, text):
                parts.append(delta)
            answer = "".join(parts) or "(empty response)"
            return web.json_response({
                "id": cid, "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                             "finish_reason": "stop"}],
            })
        except Exception as e:
            print(f"[proxy] ERROR: {e}", flush=True)
            return web.json_response({"error": {"message": str(e)}}, status=502)

    # ---- SSE streaming ----
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)  # headers out immediately

    async def send(payload):
        await resp.write(f"data: {payload}\n\n".encode())

    try:
        await send(chunk(cid, model, delta={"role": "assistant", "content": ""}))
        got = False
        async for delta in run_opencode(model, text):
            got = True
            await send(chunk(cid, model, delta=delta))
        await send(chunk(cid, model, finish="stop"))
        await send("[DONE]")
        await resp.write_eof()
        print(f"[proxy] -> done ({'ok' if got else 'EMPTY'})", flush=True)
    except asyncio.TimeoutError:
        await send(chunk(cid, model, delta="[proxy] timeout waiting for opencode"))
        await send(chunk(cid, model, finish="stop"))
        await send("[DONE]")
        await resp.write_eof()
    except Exception as e:
        print(f"[proxy] STREAM ERROR: {e}", flush=True)
        try:
            await send(chunk(cid, model, delta=f"[proxy error] {e}"))
            await send(chunk(cid, model, finish="stop"))
            await send("[DONE]")
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def handle_models(request):
    return web.json_response({"object": "list", "data": [
        {"id": m, "object": "model", "created": 0, "owned_by": "opencode"}
        for m in FREE_MODELS
    ] + [
        {"id": a, "object": "model", "created": 0, "owned_by": "opencode-alias"}
        for a in ALIASES
    ]})


async def health(request):
    return web.json_response({
        "status": "ok",
        "default_model": DEFAULT_MODEL,
        "free_models": FREE_MODELS,
        "dead_models": sorted(DEAD_MODELS),
        "cli": OPENCODE_CLI,
        "cli_exists": os.path.exists(OPENCODE_CLI),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18788)
    args = ap.parse_args()
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_post("/chat/completions", handle_chat)
    print(f"[proxy] streaming :{args.port} -> {WORK_DIR}", flush=True)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
