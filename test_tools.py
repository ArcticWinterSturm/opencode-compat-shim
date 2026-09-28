"""End-to-end tool-bridge test against the LIVE v4 shim.

Proves the model emits a protocol tool call and the shim turns it into a real
OpenAI tool_calls frame (the thing v3 could never do), and that a huge
conversation no longer dies with WinError 206.
"""
import json, urllib.request, sys

B = "http://127.0.0.1:18788"
passed = failed = 0
def ok(n, c, extra=""):
    global passed, failed
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + ("" if c else f"  {extra}"))
    if c: passed += 1
    else: failed += 1

def post(payload, stream=True, timeout=400):
    req = urllib.request.Request(
        B + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read().decode()

TERMINAL = {"type": "function", "function": {
    "name": "terminal",
    "description": "Execute a shell command and return its output.",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "The command to run."}},
        "required": ["command"]}}}

print("=== 1. model emits a real tool call (stream) ===")
raw = post({"model": "longcat", "stream": True, "tools": [TERMINAL], "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Run the shell command `echo HELLO_FROM_TOOL` and tell me what it printed."}]})
print("  raw tail:", raw[-320:].replace("\n", " ")[:320])
ok("SSE terminated with [DONE]", "data: [DONE]" in raw)
frames = [json.loads(l[6:]) for l in raw.splitlines()
          if l.startswith("data: {") and l.rstrip().endswith("}")]
calls, finish = [], None
for f in frames:
    for ch in f["choices"]:
        if ch.get("finish_reason"):
            finish = ch["finish_reason"]
        for tc in (ch.get("delta") or {}).get("tool_calls") or []:
            calls.append(tc)
ok("finish_reason == tool_calls", finish == "tool_calls", f"got {finish!r}")
ok("at least one tool_call frame", len(calls) >= 1, f"got {len(calls)}")
if calls:
    names = {c["function"].get("name") for c in calls if c["function"].get("name")}
    ok("tool name is 'terminal'", names == {"terminal"}, f"got {names}")
    argtext = "".join(c["function"].get("arguments", "") for c in calls)
    ok("arguments mention the command", "echo" in argtext and "HELLO_FROM_TOOL" in argtext,
       f"args={argtext!r}")
    ok("tool_call carries an index", all("index" in c for c in calls))
    # OpenAI streams a tool call as: first delta carries index+id+type+name,
    # a following delta carries only the arguments. So the id belongs to the
    # FIRST delta for each index, not to every delta.
    firsts, by_index = [], {}
    for c in calls:
        by_index.setdefault(c["index"], []).append(c)
    for idx, group in by_index.items():
        firsts.append(group[0])
    ok("first delta of each tool_call has an id", all(c.get("id") for c in firsts),
       f"firsts={firsts}")
    ok("first delta has type=function", all(c.get("type") == "function" for c in firsts))
    ok("each tool_call has an id", len({c["id"] for c in firsts}) == len(by_index))
    ok("indexes are 0..n-1", sorted(by_index) == list(range(len(by_index))),
       f"indexes={sorted(by_index)}")
    try:
        parsed = json.loads(argtext)
        ok("arguments parse as JSON", isinstance(parsed, dict), f"{argtext!r}")
    except Exception as e:
        ok("arguments parse as JSON", False, str(e))

print("=== 2. full tool-result round trip (model sees the result) ===")
raw2 = post({"model": "longcat", "stream": False, "tools": [TERMINAL], "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Run `echo HELLO_FROM_TOOL`."},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_x", "type": "function",
         "function": {"name": "terminal", "arguments": '{"command":"echo HELLO_FROM_TOOL"}'}}]},
    {"role": "tool", "name": "terminal", "tool_call_id": "call_x", "content": "HELLO_FROM_TOOL"},
    {"role": "user", "content": "What did the command print? Reply with only that text."}]})
body = json.loads(raw2)
msg = body["choices"][0]["message"]
ok("second turn answers in prose", bool(msg.get("content")), f"{msg}")
ok("second turn quotes the tool result", "HELLO_FROM_TOOL" in (msg.get("content") or ""),
   f"content={msg.get('content')!r}")
ok("no stray tool_call in second turn", not msg.get("tool_calls"))

print("=== 3. WinError 206 is gone (conversation far past the argv cap) ===")
big = [{"role": "system", "content": "You are a helpful assistant."}]
big.append({"role": "user", "content": "Remember this marker: PELICAN_9931"})
big += [{"role": "user", "content": f"filler line {i} " + "z" * 400} for i in range(300)]
big.append({"role": "user", "content": "What is the marker I gave you? Reply with only the marker."})
size = sum(len(str(m)) for m in big)
print(f"  (prompt is roughly {size} chars, argv cap is 32,767)")
ok("test prompt exceeds the Windows argv cap", size > 32767, f"size={size}")
raw3 = post({"model": "longcat", "stream": False, "messages": big})
b3 = json.loads(raw3)
c3 = (b3["choices"][0]["message"].get("content") or "")
ok("no WinError 206 in response", "206" not in c3 and "too long" not in c3.lower(), f"{c3[:200]}")
ok("recalled the marker from a huge context", "PELICAN_9931" in c3, f"content={c3[:200]}")

print(f"\n================ {passed} passed, {failed} failed ================")
sys.exit(1 if failed else 0)
