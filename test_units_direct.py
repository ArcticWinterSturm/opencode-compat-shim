#!/usr/bin/env python3
"""Offline unit tests for the CLI-free direct proxy.

No network. Exercises the pure functions: model resolution, body shaping,
header construction, session-id format, and non-stream SSE reassembly.
Run: python test_units.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode_direct_proxy as p

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


print("== model resolution ==")
check("default when empty", p.resolve_model("") == p.DEFAULT_MODEL)
check("strip opencode/ prefix", p.resolve_model("opencode/space-bunny-free") == "space-bunny-free")
check("strip opencode-go/ prefix", p.resolve_model("opencode-go/longcat") == "longcat-2.5-preview-free")
check("alias bunny", p.resolve_model("bunny") == "space-bunny-free")
check("alias case-insensitive", p.resolve_model("LongCat") == "longcat-2.5-preview-free")
check("unknown passthrough", p.resolve_model("gpt-5.5") == "gpt-5.5")

print("== gate tools ==")
check("gate tools loaded", len(p.GATE_TOOLS) >= 6, f"got {len(p.GATE_TOOLS)}")
names = {t["function"]["name"] for t in p.GATE_TOOLS}
check("gate has read (mandatory)", "read" in names)
check("gate has shell (mandatory)", "shell" in names)
check("gate tools are function-shaped",
      all(t.get("type") == "function" and "name" in t.get("function", {}) for t in p.GATE_TOOLS))

print("== session id format ==")
sid = p.mint_session_id()
check("session starts ses_", sid.startswith("ses_"))
check("session length 30", len(sid) == 30, f"got {len(sid)}")
check("session hex part", all(c in "0123456789abcdef" for c in sid[4:16]))
b62 = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
check("session b62 part", all(c in b62 for c in sid[16:]))
check("session ids unique", p.mint_session_id() != p.mint_session_id())

print("== headers ==")
h = p.build_upstream_headers()
check("bearer public", h["Authorization"] == "Bearer public")
check("UA harness shape", h["User-Agent"] == "opencode/latest/2.0.11/cli")
check("project 40 hex", len(h["x-opencode-project"]) == 40)
for hk in ("b3", "traceparent", "x-opencode-client", "x-opencode-session",
           "x-session-affinity", "x-session-id"):
    check(f"header {hk} present", hk in h)
check("b3 4-part", len(h["b3"].split("-")) == 4)
check("traceparent 4-part", len(h["traceparent"].split("-")) == 4)
check("traceparent starts 00", h["traceparent"].startswith("00-"))
check("session headers agree",
      h["x-opencode-session"] == h["x-session-affinity"] == h["x-session-id"])

print("== body shaping ==")
body = {"model": "space-bunny-free",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "mytool", "parameters": {}}}]}
out = p.prepare_body(body, "space-bunny-free")
check("forces stream true", out["stream"] is True)
check("include_usage", out["stream_options"] == {"include_usage": True})
check("model rewritten", out["model"] == "space-bunny-free")
onames = [t["function"]["name"] for t in out["tools"]]
check("caller tool kept", "mytool" in onames)
check("gate tools appended", "read" in onames and "shell" in onames)
check("caller tool first", onames[0] == "mytool")

# caller already defines read -> caller's wins, no duplicate
body2 = {"model": "space-bunny-free", "messages": [],
         "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}
out2 = p.prepare_body(body2, "space-bunny-free")
n2 = [t["function"]["name"] for t in out2["tools"]]
check("no duplicate read", n2.count("read") == 1, f"got {n2}")

# no caller tools -> gate tools alone
out3 = p.prepare_body({"model": "x", "messages": []}, "x")
check("gate-only when no caller tools", "read" in [t["function"]["name"] for t in out3["tools"]])

# non-stream request still gets stream:true forced upstream (gate requirement)
out4 = p.prepare_body({"model": "x", "messages": [], "stream": False}, "x")
check("non-stream forced to stream upstream", out4["stream"] is True)

print("== dead models ==")
check("mimo-v2.5 dead", "mimo-v2.5-free" in p.DEAD_MODELS)
check("space-bunny alive", "space-bunny-free" not in p.DEAD_MODELS)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
