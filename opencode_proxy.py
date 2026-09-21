#!/usr/bin/env python3
"""OpenAI-compatible SSE-streaming proxy over `opencode run`.

Fixes vs v1:
- Streams deltas as opencode emits them (Hermes requires streaming; v1 buffered
  everything -> "empty stream with no finish_reason").
- Sends an initial role chunk immediately so clients see headers fast.
- Handles both /v1/chat/completions and /chat/completions.
- Non-stream requests still get a complete JSON response.
"""

import asyncio, json, uuid, argparse, sys
from aiohttp import web

OPENCODE_CLI = r"C:\Users\User\AppData\Roaming\ai.opencode.desktop\cli\2.0.11\opencode-cli.exe"
WORK_DIR = r"C:\Users\User"
PER_READ_TIMEOUT = 180  # seconds between opencode output lines


async def run_opencode(model, text):
    """Yield text deltas as opencode emits them."""
    cmd = [OPENCODE_CLI, "run", "--format", "json", "--model", f"opencode/{model}", text]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=WORK_DIR
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
        await proc.kill() if proc.returncode is None else None
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
    model can react to them.
    """
    lines = []
    for m in msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # multimodal parts -> text only
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
    model = body.get("model", "big-pickle")
    stream = body.get("stream", False)
    msgs = body.get("messages", [])
    text = flatten_history(msgs) or "Hello"

    print(f"[proxy] {'stream' if stream else 'block '} {model}: {text[:60]!r}", flush=True)
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
        {"id": m, "object": "model"} for m in [
            "big-pickle", "muse-spark-1.3-contributor-free",
            "muse-spark-1.2-contributor-free", "mimo-v2.5-free",
            "deepseek-v4-flash-free",
        ]
    ]})


async def health(request):
    return web.json_response({"status": "ok"})


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
