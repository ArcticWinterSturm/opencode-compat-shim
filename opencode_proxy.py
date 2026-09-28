#!/usr/bin/env python3
"""OpenAI-shaped SSE proxy that tunnels OpenCode free-tier models via its CLI.

OpenCode's free tier is locked behind desktop-app identity: bare HTTP gets 403.
The only authenticated transport is the signed CLI, which speaks a custom
per-call JSON protocol. This proxy translates OpenAI <-> that protocol.

Two hard problems this solves, both verified empirically (2026-09-28):

1. WinError 206 ("filename or extension is too long").
   The prompt used to ride in argv, and Windows caps a command line at 32,767
   characters. Any real conversation blows that instantly. The prompt now goes
   in over STDIN, which has no such limit. (`--file` does NOT work on its own:
   the CLI rejects it with "You must provide a message".)

2. Tool calling.
   The shim used to drop the OpenAI `tools` array entirely, so models behind it
   could not drive Hermes tools and would hallucinate results. The full tool
   catalogue is now serialised into the prompt, the model emits a fenced
   tool-call block, and the shim parses it back into real OpenAI `tool_calls`
   frames. Hermes executes the tools; the shim never does.

   The CLI injects its OWN tools (bash/read/write) that do not exist in Hermes
   and cannot be disabled in this build (custom agent definitions in
   opencode.json / agent/*.md are not loaded by opencode-cli 2.0.11). Those
   `tool_use` events are therefore SUPPRESSED: never forwarded to Hermes, and
   the model is told up front it has no built-in tools. If it reaches for one
   anyway, the shim injects a corrective nudge instead of leaking an alien
   tool call.
"""

import asyncio, json, os, uuid, argparse
from aiohttp import web

OPENCODE_CLI = os.path.expandvars(
    r"%APPDATA%\ai.opencode.desktop\cli\2.0.11\opencode-cli.exe"
)
WORK_DIR = os.path.expandvars(r"%USERPROFILE%\Documents")
PER_READ_TIMEOUT = 300
DEFAULT_MODEL = "longcat-2.5-preview-free"

FREE_MODELS = [
    "longcat-2.5-preview-free",
    "space-bunny-free",
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
ALIASES = {
    "longcat": "longcat-2.5-preview-free", "longcat-2.5": "longcat-2.5-preview-free",
    "bunny": "space-bunny-free", "space-bunny": "space-bunny-free",
    "mimo": "mimo-v2.6-flash-free", "ling": "ling-3.0-flash-fin-free",
    "nemotron": "nemotron-3-ultra-free", "nemotron-3-ultra": "nemotron-3-ultra-free",
}

OPEN_FENCE = "<hermes_tool_call>"
CLOSE_FENCE = "</hermes_tool_call>"

PROTOCOL = """\
# TOOL PROTOCOL (read carefully)

You do NOT have any built-in tools. Anything you think you can do with a
built-in shell, file reader or editor does not exist here, and its output is
discarded. You have exactly the tools listed below, provided by an external
harness that executes them for you and returns the results.

To call one or more tools, output ONLY this fenced block, with no other text:

<hermes_tool_call>
{"name": "TOOL_NAME", "arguments": {"KEY": "VALUE"}}
</hermes_tool_call>

Rules:
- The block must be the entire response. No prose before or after it.
- `arguments` must be a JSON object matching that tool's `parameters` schema.
- To call several tools at once, put one JSON object per line inside the single
  block. They are executed in parallel.
- Use EXACT tool names as written below.
- When you already have everything you need, reply with plain prose and NO block.
"""


def resolve_model(name):
    name = (name or DEFAULT_MODEL).strip()
    for p in ("opencode-go/", "opencode/"):
        if name.startswith(p):
            name = name[len(p):]
    return ALIASES.get(name.lower(), name)


def cli_env():
    """Subprocess env with a native PWD.

    opencode-cli.exe chdir()s to $PWD. Under Git-Bash/MSYS that is a POSIX path
    ('/c/Users/...') the Windows binary cannot chdir into, and every call dies
    with 'Failed to change directory to /c/Users/...'.
    """
    env = dict(os.environ)
    env["PWD"] = WORK_DIR
    return env


# ── tool catalogue ───────────────────────────────────────────────────────────

def render_tools(tools):
    """Serialise the OpenAI tools array into a compact prompt catalogue."""
    if not tools:
        return ""
    out = ["# AVAILABLE TOOLS"]
    for t in tools:
        fn = t.get("function") or {}
        out.append(f"## {fn.get('name')}")
        if fn.get("description"):
            out.append(fn["description"].strip())
        schema = fn.get("parameters") or {}
        props = schema.get("properties") or {}
        if props:
            out.append("parameters (JSON Schema):")
            out.append(json.dumps(
                {"type": schema.get("type", "object"),
                 "properties": props,
                 "required": schema.get("required", [])},
                ensure_ascii=False, separators=(",", ":")))
        out.append("")
    return "\n".join(out)


def _content_to_text(content):
    if isinstance(content, list):
        bits = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("image_url", "image"):
                bits.append("[image supplied; this transport is text-only]")
            else:
                bits.append(p.get("text") or "")
        return " ".join(b for b in bits if b)
    return content or ""


def flatten_history(msgs):
    """Flatten the whole Hermes conversation into one prompt.

    The CLI is a fresh process per call, so context must ride in the prompt.
    Tool calls and their results are labelled so the model can chain them.
    """
    lines = []
    for m in msgs:
        role = m.get("role", "user")
        if role == "tool":
            name = m.get("name") or m.get("tool_call_id") or "tool"
            lines.append(f"[tool result: {name}]\n{_content_to_text(m.get('content'))}")
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            try:
                args = json.dumps(json.loads(args)) if isinstance(args, str) else args
            except Exception:
                pass
            lines.append(
                f"[tool call: {fn.get('name')}]\narguments: {json.dumps(args, ensure_ascii=False)}")
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        if role == "system":
            lines.append(f"[system]\n{text}")
        elif role == "assistant":
            lines.append(f"[assistant]\n{text}")
        else:
            lines.append(f"[user]\n{text}")
    return "\n\n".join(lines).strip()


def build_prompt(body):
    msgs = body.get("messages", [])
    tools = body.get("tools") or []
    parts = []
    if tools:
        parts.append(PROTOCOL)
        parts.append(render_tools(tools))
    convo = flatten_history(msgs)
    parts.append(convo if convo else "Hello")
    return "\n\n".join(p for p in parts if p)


# ── tool-call stream splitting ───────────────────────────────────────────────

def _partial_suffix_len(buf, fence):
    """Longest k < len(fence) such that buf ends with fence[:k]."""
    maxk = min(len(fence) - 1, len(buf))
    for k in range(maxk, 0, -1):
        if buf.endswith(fence[:k]):
            return k
    return 0


class Splitter:
    """Split model text into prose and fenced tool-call blocks, streaming-safe.

    Yields ('text', s) / ('tool', s). Holds back only enough trailing text that
    a fence delimiter cannot straddle a chunk boundary, so prose still streams.
    """

    def __init__(self):
        self.buf = ""
        self.in_tool = False

    def feed(self, new):
        self.buf += new
        while self.buf:
            if self.in_tool:
                i = self.buf.find(CLOSE_FENCE)
                if i >= 0:
                    if i:
                        yield ("tool", self.buf[:i])
                    self.buf = self.buf[i + len(CLOSE_FENCE):]
                    self.in_tool = False
                    continue
                keep = _partial_suffix_len(self.buf, CLOSE_FENCE)
                if len(self.buf) > keep:
                    cut = len(self.buf) - keep
                    yield ("tool", self.buf[:cut])
                    self.buf = self.buf[cut:]
                return
            i = self.buf.find(OPEN_FENCE)
            if i >= 0:
                if i:
                    yield ("text", self.buf[:i])
                self.buf = self.buf[i + len(OPEN_FENCE):]
                self.in_tool = True
                continue
            keep = _partial_suffix_len(self.buf, OPEN_FENCE)
            if len(self.buf) > keep:
                cut = len(self.buf) - keep
                yield ("text", self.buf[:cut])
                self.buf = self.buf[cut:]
            return

    def flush(self):
        """Emit whatever is left when the model stops."""
        if self.buf:
            yield ("tool" if self.in_tool else "text", self.buf)
            self.buf = ""
        self.in_tool = False


def parse_tool_block(raw):
    """Parse the inside of a tool fence into a list of OpenAI tool calls."""
    raw = raw.strip()
    if not raw:
        return []
    obj = None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        objs = []
        for line in raw.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        obj = objs or None
    if obj is None:
        return []
    items = obj if isinstance(obj, list) else [obj]
    calls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("tool") or it.get("function")
        if isinstance(name, dict):
            name = name.get("name")
        if not name:
            continue
        args = it.get("arguments", it.get("parameters", it.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"input": args}
        if not isinstance(args, dict):
            args = {"value": args}
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": str(name),
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


# ── CLI transport ────────────────────────────────────────────────────────────

class ShimError(Exception):
    pass


async def run_opencode(model, prompt):
    """Yield events: ('text', delta) | ('tool', raw) | ('alien_tool', name).

    The prompt is written to STDIN, never argv (WinError 206).
    """
    cmd = [OPENCODE_CLI, "run", "--format", "json",
           "--model", "opencode/" + model]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=WORK_DIR, env=cli_env(),
    )
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

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
            t = obj.get("type")
            if t == "error":
                err = obj.get("error", {})
                raise ShimError(f"{err.get('type', 'error')}: {err.get('message', '?')}")
            if t == "tool_use":
                # OpenCode's own tools. Not Hermes tools - suppress.
                yield ("alien_tool", (obj.get("part") or {}).get("tool", "?"))
            elif t == "text":
                part = obj.get("part", {})
                new = part.get("text", obj.get("text", ""))
                if new and len(new) > len(sent):
                    yield ("text", new[len(sent):])
                    sent = new
        rc = await proc.wait()
        if rc != 0:
            err = (await proc.stderr.read()).decode("utf-8", "replace")[-500:]
            raise ShimError(f"opencode exit {rc}: {err}")

    try:
        async for ev in pump():
            yield ev
    except Exception:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise


async def collect(model, prompt, stream_cb=None):
    """Run one turn. Returns (text, tool_calls, alien_tool_names).

    If the model reached for one of the CLI's own tools, the turn is retried
    once with a corrective nudge appended to the prompt.
    """
    alien = []
    for attempt in range(2):
        sp = Splitter()
        text_parts, tool_raw = [], []
        alien = []
        async for ev, payload in run_opencode(model, prompt):
            if ev == "text":
                for kind, seg in sp.feed(payload):
                    if kind == "text":
                        text_parts.append(seg)
                        if stream_cb:
                            await stream_cb(seg)
                    else:
                        tool_raw.append(seg)
            elif ev == "alien_tool":
                alien.append(payload)
        for kind, seg in sp.flush():
            if kind == "text":
                text_parts.append(seg)
                if stream_cb:
                    await stream_cb(seg)
            else:
                tool_raw.append(seg)

        calls = parse_tool_block("".join(tool_raw))
        if calls or not alien or attempt == 1:
            return "".join(text_parts), calls, alien
        # The model tried a tool that does not exist in Hermes. Correct it.
        prompt = (prompt + "\n\n[system] Your last attempt tried to use a built-in "
                  "tool (" + ", ".join(sorted(set(alien))) + "). That tool does not "
                  "exist here and its output is discarded. Use the tool protocol "
                  "above with one of the listed tools, or answer in plain text.")
    return "", [], []


# ── SSE plumbing ─────────────────────────────────────────────────────────────

def chunk(cid, model, delta=None, finish=None):
    d = {}
    if delta is not None:
        d["delta"] = delta
    if finish:
        d["finish_reason"] = finish
    return json.dumps({"id": cid, "object": "chat.completion.chunk",
                       "created": 0, "model": model,
                       "choices": [{"index": 0, **d}]})


def tool_call_chunks(cid, model, calls):
    """OpenAI streaming shape for tool calls: index, id, name, then arguments."""
    out = []
    for i, c in enumerate(calls):
        out.append(json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i, "id": c["id"], "type": "function",
                "function": {"name": c["function"]["name"], "arguments": ""}}]}}]}))
        out.append(json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i,
                "function": {"arguments": c["function"]["arguments"]}}]}}]}))
    return out


def final_body(cid, model, text, calls):
    msg = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = calls
    return {"id": cid, "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


# ── handlers ─────────────────────────────────────────────────────────────────

async def handle_chat(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)

    requested = body.get("model") or DEFAULT_MODEL
    model = resolve_model(requested)
    if model in DEAD_MODELS:
        return web.json_response(
            {"error": {"message": DEAD_MODELS[model], "type": "model_not_found",
                       "code": "model_dead", "param": "model"}}, status=503)

    stream = bool(body.get("stream"))
    ntools = len(body.get("tools") or [])
    prompt = build_prompt(body)
    print(f"[proxy] {requested}->{model} {len(prompt)}ch "
          f"{'stream' if stream else 'block'} tools={ntools}", flush=True)
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]

    if not stream:
        try:
            text, calls, alien = await collect(model, prompt)
            return web.json_response(final_body(cid, model, text, calls))
        except Exception as e:
            print(f"[proxy] ERROR: {e}", flush=True)
            return web.json_response({"error": {"message": str(e)}}, status=502)

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    await resp.prepare(request)

    async def send(payload):
        await resp.write(f"data: {payload}\n\n".encode())

    try:
        await send(chunk(cid, model, delta={"role": "assistant", "content": ""}))

        async def on_text(seg):
            await send(chunk(cid, model, delta={"content": seg}))

        text, calls, alien = await collect(model, prompt, stream_cb=on_text)
        for payload in tool_call_chunks(cid, model, calls):
            await send(payload)
        await send(chunk(cid, model, finish="tool_calls" if calls else "stop"))
        await send("[DONE]")
        await resp.write_eof()
        print(f"[proxy] -> done text={len(text)}ch tools={len(calls)} alien={alien}", flush=True)
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
        for m in FREE_MODELS] + [
        {"id": a, "object": "model", "created": 0, "owned_by": "opencode-alias"}
        for a in ALIASES]})


async def health(request):
    return web.json_response({
        "status": "ok", "default_model": DEFAULT_MODEL, "free_models": FREE_MODELS,
        "dead_models": sorted(DEAD_MODELS), "transport": "stdin",
        "tool_bridge": True, "cli": OPENCODE_CLI, "cli_exists": os.path.exists(OPENCODE_CLI),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18788)
    args = ap.parse_args()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_post("/chat/completions", handle_chat)
    print(f"[proxy] streaming :{args.port} -> {WORK_DIR}", flush=True)
    print(f"[proxy] cli exists: {os.path.exists(OPENCODE_CLI)}", flush=True)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
