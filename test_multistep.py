"""Multi-step and parallel tool calls against the LIVE shim.

The round trip Hermes actually performs: model calls a tool, Hermes runs it,
the result crosses back, the model calls another tool with what it learned.
"""
import json, urllib.request, sys

B = "http://127.0.0.1:18788"
passed = failed = 0
def ok(n, c, extra=""):
    global passed, failed
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + ("" if c else f"  {extra}"))
    if c: passed += 1
    else: failed += 0
    if not c: failed += 1

def post(payload, timeout=400):
    req = urllib.request.Request(B + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read().decode())

T = lambda n, d, props, req=(): {"type": "function", "function": {
    "name": n, "description": d,
    "parameters": {"type": "object", "properties": props, "required": list(req)}}}

TOOLS = [
    T("terminal", "Execute a shell command and return its output.",
      {"command": {"type": "string", "description": "The command to run."}}, ["command"]),
    T("read_file", "Read a file from disk and return its contents.",
      {"path": {"type": "string", "description": "Absolute file path."}}, ["path"]),
]

print("=== 1. multi-step: result of tool 1 feeds tool 2 ===")
user_msg = ("Use terminal to write the number 4242 into a file at "
            "C:\\Users\\User\\AppData\\Local\\Temp\\ms1.txt, then use "
            "read_file to read that same file back, then tell me the "
            "number it contained.")
msgs = [{"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user_msg}]
seen_names, turns = [], 0
while turns < 6:
    turns += 1
    r = post({"model": "longcat", "stream": False, "tools": TOOLS, "messages": msgs})
    ch = r["choices"][0]
    tcs = ch["message"].get("tool_calls") or []
    if not tcs:
        break
    msgs.append({"role": "assistant", "content": ch["message"].get("content"), "tool_calls": tcs})
    for t in tcs:
        a = json.loads(t["function"]["arguments"])
        name = t["function"]["name"]
        seen_names.append(name)
        if name == "terminal":
            import subprocess as sp
            sp.run(["cmd", "/c", a.get("command", "")], capture_output=True, text=True)
            val = "(command completed, no stdout)"
        else:
            try:
                val = open(a["path"]).read().strip()
            except Exception as e:
                val = f"error: {e}"
        msgs.append({"role": "tool", "name": name, "tool_call_id": t["id"], "content": val})

final = (r["choices"][0]["message"].get("content") or "")
print(f"  chain: {' -> '.join(seen_names)}  (turns={turns})")
print("  final answer:", repr(final[:140]))
ok("chain called terminal then read_file",
   seen_names[:2] == ["terminal", "read_file"], f"{seen_names}")
ok("model read the file back through the bridge", "read_file" in seen_names, f"{seen_names}")
ok("final turn is prose, not a tool call", r["choices"][0]["finish_reason"] == "stop")
ok("final answer reports the real file content (4242)", "4242" in final, f"{final[:150]!r}")

print("=== 2. parallel tool calls in one turn ===")
r3 = post({"model": "longcat", "stream": False, "tools": TOOLS, "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Run these two commands in one turn: "
                                "`echo ALPHA_MARK` and `echo BETA_MARK`. Then tell me both words."}]})
tcs3 = r3["choices"][0]["message"].get("tool_calls") or []
print("  calls:", [(t["function"]["name"], t["function"]["arguments"][:70]) for t in tcs3])
ok("turn requests tool calls", r3["choices"][0]["finish_reason"] == "tool_calls")
ok("more than one tool call requested", len(tcs3) >= 2, f"got {len(tcs3)}")
if len(tcs3) >= 2:
    ok("each call has a distinct id", len({t["id"] for t in tcs3}) == len(tcs3))
    allargs = " ".join(t["function"]["arguments"] for t in tcs3)
    ok("both markers appear in the arguments",
       "ALPHA_MARK" in allargs and "BETA_MARK" in allargs, allargs[:200])

print("=== 3. no-tools request still behaves like plain chat ===")
r4 = post({"model": "longcat", "stream": False, "messages": [
    {"role": "user", "content": "Reply with exactly: NO_TOOLS_FINE"}]})
c4 = (r4["choices"][0]["message"].get("content") or "")
ok("plain chat still works", "NO_TOOLS_FINE" in c4, f"{c4[:120]!r}")
ok("finish_reason is stop", r4["choices"][0]["finish_reason"] == "stop")

print(f"\n================ {passed} passed, {failed} failed ================")
sys.exit(1 if failed else 0)
