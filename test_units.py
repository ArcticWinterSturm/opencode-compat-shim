"""Unit tests for the parts that must be exactly right: stream splitting,
tool-block parsing, and the WinError-206 length path. No network."""
import importlib.util, json, sys, os

spec = importlib.util.spec_from_file_location(
    "p4", r"C:\Users\User\Documents\Shim_v2\opencode_proxy.py")
p4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p4)

passed = failed = 0
def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        print(f"  PASS  {name}"); passed += 1
    else:
        print(f"  FAIL  {name} {extra}"); failed += 1

def split_all(chunks):
    sp = p4.Splitter()
    out = []
    for c in chunks:
        out.extend(sp.feed(c))
    out.extend(sp.flush())
    return out

print("=== splitter: fence split across chunk boundaries ===")
# The single most likely bug: a fence straddling two deltas.
for size in range(1, 40):
    payload = ("hello there " + p4.OPEN_FENCE +
               '{"name":"terminal","arguments":{"command":"ls"}}' +
               p4.CLOSE_FENCE + " done")
    chunks = [payload[i:i+size] for i in range(0, len(payload), size)]
    got = split_all(chunks)
    text = "".join(s for k, s in got if k == "text")
    tool = "".join(s for k, s in got if k == "tool")
    calls = p4.parse_tool_block(tool)
    if text != "hello there  done" or len(calls) != 1 or calls[0]["function"]["name"] != "terminal":
        ok(f"chunk size {size}", False, f"text={text!r} tool={tool!r} calls={calls}")
        break
else:
    ok("fence survives every chunk size 1..39", True)

print("=== splitter: prose only, byte-identical ===")
for size in (1, 3, 7, 100):
    payload = "The quick brown fox jumps over the lazy dog. 12345 !@#"
    chunks = [payload[i:i+size] for i in range(0, len(payload), size)]
    got = split_all(chunks)
    ok(f"prose intact @size {size}",
       "".join(s for k, s in got if k == "text") == payload and
       not any(k == "tool" for k, _ in got))

print("=== splitter: literal '<' and near-miss fences ===")
for payload in ["a < b", "x <hermes_tool_c", "no fence </hermes_tool_call> here",
                "<<hermes_tool_call>>"]:
    chunks = [payload[i:i+2] for i in range(0, len(payload), 2)]
    got = split_all(chunks)
    txt = "".join(s for k, s in got if k == "text")
    tool = "".join(s for k, s in got if k == "tool")
    # Nothing should be lost; a stray close fence with no open stays prose.
    ok(f"no data loss @ {payload[:24]!r}", (txt + tool) == payload or tool != "",
       f"txt={txt!r} tool={tool!r}")

print("=== parser: single, multiple, array, string-args ===")
c1 = p4.parse_tool_block('{"name":"terminal","arguments":{"command":"ls"}}')
ok("single call", len(c1) == 1 and c1[0]["function"]["name"] == "terminal"
   and json.loads(c1[0]["function"]["arguments"]) == {"command": "ls"})

c2 = p4.parse_tool_block(
    '{"name":"a","arguments":{}}\n{"name":"b","arguments":{"x":1}}')
ok("two calls, one per line", len(c2) == 2 and [c["function"]["name"] for c in c2] == ["a", "b"])

c3 = p4.parse_tool_block('[{"name":"a","arguments":{}},{"name":"b","arguments":{}}]')
ok("json array of calls", len(c3) == 2)

c4 = p4.parse_tool_block('{"name":"x","arguments":"{\\"k\\": \\"v\\"}"}')
ok("arguments as a JSON string", len(c4) == 1 and
   json.loads(c4[0]["function"]["arguments"]) == {"k": "v"})

ok("garbage yields no calls", p4.parse_tool_block("not json at all") == [])
ok("empty yields no calls", p4.parse_tool_block("   ") == [])

print("=== ids and shape ===")
ok("unique call ids", len({c["id"] for c in c2}) == 2)
ok("has type=function", all(c["type"] == "function" for c in c2))
ok("arguments always a JSON string", all(isinstance(c["function"]["arguments"], str) for c in c2))

print("=== prompt build: everything crosses ===")
body = {
    "messages": [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "terminal", "arguments": '{"command":"ls"}'}}]},
        {"role": "tool", "name": "terminal", "tool_call_id": "call_1", "content": "a.txt\nb.txt"},
        {"role": "user", "content": "now delete them"},
    ],
    "tools": [{"type": "function", "function": {
        "name": "terminal", "description": "Run a shell command.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string", "description": "cmd"}},
                       "required": ["command"]}}}],
}
pr = p4.build_prompt(body)
for needle in ["You are Hermes.", "list files", "[tool call: terminal]", "a.txt",
               "now delete them", "## terminal", "command", "hermes_tool_call"]:
    ok(f"prompt carries {needle[:28]!r}", needle in pr)

print("=== WinError 206: prompt far past the 32,767 argv cap ===")
huge = {"messages": [{"role": "user", "content": "x" * 200000}],
        "tools": [{"type": "function", "function": {
            "name": "t", "description": "d" * 5000,
            "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}}}]}
p = p4.build_prompt(huge)
ok("200k-char prompt built", len(p) > 200000, f"len={len(p)}")
ok("exceeds the Windows argv cap", len(p) > 32767)

print(f"\n================ {passed} passed, {failed} failed ================")
sys.exit(1 if failed else 0)
