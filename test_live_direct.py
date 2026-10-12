#!/usr/bin/env python3
"""Live integration test for the CLI-free direct proxy.

Hits a running proxy (default http://127.0.0.1:18799) and exercises the full
OpenAI contract against the real Zen free tier: health, models, non-stream
chat, streaming chat, native tool calling (non-stream + stream), multi-turn
tool results, and the dead-model 503 path.

Start the proxy first:
    python opencode_direct_proxy.py --port 18799
Then:
    python test_live_direct.py [--base http://127.0.0.1:18799] [--model space-bunny-free]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def post(base, path, body, timeout=120):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(base, path, timeout=15):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def sse_text(raw):
    """Pull assistant content out of an SSE chat.completion stream."""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        for ch in obj.get("choices", []):
            d = ch.get("delta", {})
            if d.get("content"):
                out.append(d["content"])
    return "".join(out)


def sse_tool_calls(raw):
    """Pull (name, arguments) out of streamed tool_calls deltas."""
    calls = {}
    finish = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        for ch in obj.get("choices", []):
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            for tc in ch.get("delta", {}).get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                slot = calls.setdefault(idx, {"name": "", "arguments": ""})
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
    return calls, finish


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--model", default="space-bunny-free")
    args = ap.parse_args()
    base, model = args.base, args.model

    print(f"== health ({base}) ==")
    st, raw = get(base, "/health")
    check("health 200", st == 200, f"got {st}")
    if st == 200:
        h = json.loads(raw)
        check("cli_required false", h.get("cli_required") is False)
        check("transport direct-http", h.get("transport") == "direct-http")
        check("gate has read+shell",
              "read" in h.get("gate_tools", []) and "shell" in h.get("gate_tools", []))

    print("== models ==")
    st, raw = get(base, "/v1/models")
    check("models 200", st == 200, f"got {st}")
    if st == 200:
        ids = [m["id"] for m in json.loads(raw)["data"]]
        check("space-bunny listed", "space-bunny-free" in ids)
        check("alias bunny listed", "bunny" in ids)

    print("== non-stream chat ==")
    st, raw = post(base, "/v1/chat/completions", {
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}]})
    check("non-stream 200", st == 200, f"got {st}: {raw[:120]}")
    if st == 200:
        d = json.loads(raw)
        content = (d["choices"][0]["message"].get("content") or "").strip()
        check("content is PONG", "PONG" in content, f"got {content[:60]!r}")
        check("finish stop", d["choices"][0]["finish_reason"] == "stop")
        check("usage present", d.get("usage", {}).get("total_tokens", 0) > 0)

    print("== streaming chat ==")
    req = urllib.request.Request(base + "/v1/chat/completions",
        data=json.dumps({"model": model, "stream": True,
                         "messages": [{"role": "user", "content": "Count: 1 2 3"}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
        text = sse_text(raw)
        check("stream produced text", len(text) > 0, f"got {text[:40]!r}")
        check("stream has [DONE]", "[DONE]" in raw)
    except Exception as e:
        check("stream request", False, str(e)[:100])

    print("== tool calling (non-stream) ==")
    st, raw = post(base, "/v1/chat/completions", {
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": "What is the weather in Paris? Use get_weather."}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "Get weather for a city",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]})
    check("tool-call 200", st == 200, f"got {st}: {raw[:120]}")
    if st == 200:
        d = json.loads(raw)
        calls = d["choices"][0]["message"].get("tool_calls") or []
        check("one tool call emitted", len(calls) >= 1, f"got {len(calls)}")
        if calls:
            check("tool name correct", calls[0]["function"]["name"] == "get_weather")
            try:
                a = json.loads(calls[0]["function"]["arguments"])
                check("tool args valid json", isinstance(a, dict))
                check("tool arg city=Paris", a.get("city", "").lower() == "paris", f"got {a}")
            except Exception as e:
                check("tool args valid json", False, str(e))
        check("finish tool_calls", d["choices"][0]["finish_reason"] == "tool_calls")

    print("== tool calling (stream) ==")
    req = urllib.request.Request(base + "/v1/chat/completions",
        data=json.dumps({"model": model, "stream": True,
            "messages": [{"role": "user", "content": "Call add with a=2 and b=3."}],
            "tools": [{"type": "function", "function": {
                "name": "add", "description": "Add two numbers",
                "parameters": {"type": "object",
                               "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}}}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
        calls, finish = sse_tool_calls(raw)
        check("stream tool call emitted", len(calls) >= 1, f"got {calls}")
        if calls:
            check("stream tool name add", calls[0]["name"] == "add")
        check("stream finish tool_calls", finish == "tool_calls", f"got {finish}")
    except Exception as e:
        check("stream tool request", False, str(e)[:100])

    print("== multi-turn tool result ==")
    st, raw = post(base, "/v1/chat/completions", {
        "model": model, "stream": False,
        "messages": [
            {"role": "user", "content": "What is 2+3? Use the add tool."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "add", "arguments": "{\"a\":2,\"b\":3}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "5"}],
        "tools": [{"type": "function", "function": {
            "name": "add", "description": "Add two numbers",
            "parameters": {"type": "object",
                           "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}}}]})
    check("multi-turn 200", st == 200, f"got {st}")
    if st == 200:
        d = json.loads(raw)
        content = (d["choices"][0]["message"].get("content") or "")
        check("model used tool result", "5" in content, f"got {content[:60]!r}")

    print("== dead model -> 503 ==")
    st, raw = post(base, "/v1/chat/completions", {
        "model": "mimo-v2.5-free", "messages": [{"role": "user", "content": "hi"}]})
    check("dead model 503", st == 503, f"got {st}")
    if st == 503:
        check("dead model code", json.loads(raw)["error"].get("code") == "model_dead")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
