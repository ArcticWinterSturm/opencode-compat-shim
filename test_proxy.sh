#!/usr/bin/env bash
# End-to-end acceptance test for the OpenCode compat shim.
# 1. SSE streaming shape (the thing Hermes actually needs)
# 2. Conversation memory across turns (CLI is stateless)
# 3. Every registered free model answers
# 4. Dead model returns a clean 503, not an opaque 502
set -u
B="http://127.0.0.1:18788"
pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "=== 1. /v1/models ==="
n=$(curl -s -m 10 "$B/v1/models" | grep -o '"id"' | wc -l)
[ "$n" -ge 6 ] && ok "models listed: $n entries" || bad "models listed: $n entries"

echo "=== 2. SSE streaming + memory (one call, 2 turns of history) ==="
out=$(curl -s -N -m 180 -X POST "$B/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"longcat","stream":true,"messages":[
       {"role":"system","content":"You are a terse assistant."},
       {"role":"user","content":"My favorite color is teal. Remember it."},
       {"role":"assistant","content":"Noted - teal."},
       {"role":"user","content":"What is my favorite color? One word only."}]}' 2>&1)

echo "$out" | head -c 300; echo; echo "  ---"
echo "$out" | grep -q 'data: \[DONE\]'      && ok "[DONE] sentinel"        || bad "[DONE] sentinel"
# json.dumps emits `"key": "value"` with a space - match whitespace-tolerantly.
echo "$out" | grep -qE '"finish_reason"[[:space:]]*:[[:space:]]*"stop"' \
  && ok "finish_reason stop"  || bad "finish_reason stop"
echo "$out" | grep -qE '"role"[[:space:]]*:[[:space:]]*"assistant"' \
  && ok "role chunk first"    || bad "role chunk first"
# The role chunk must be the FIRST frame, or Hermes waits on headers.
first=$(echo "$out" | grep -m1 '^data: {"id"')
echo "$first" | grep -qE '"role"[[:space:]]*:[[:space:]]*"assistant"' \
  && ok "role chunk is frame #1" || bad "role chunk is frame #1 (got: ${first:0:80})"
frames=$(echo "$out" | grep -c '^data: {"id"')
[ "$frames" -ge 3 ] && ok "streamed $frames frames (not 1 blob)" || bad "streamed $frames frames"
echo "$out" | grep -qi 'teal' && ok "REMEMBERED context across turns" || bad "REMEMBERED context across turns"

echo "=== 3. every free model answers (non-stream) ==="
for m in longcat-2.5-preview-free space-bunny-free mimo-v2.6-flash-free \
         ling-3.0-flash-fin-free nemotron-3-ultra-free nemotron-3.5-lightning-free; do
  r=$(curl -s -m 180 -X POST "$B/v1/chat/completions" -H "Content-Type: application/json" \
      -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}]}")
  if echo "$r" | grep -q '"finish_reason"'; then
    ok "$m -> $(echo "$r" | sed 's/.*"content": *"\([^"]*\)".*/\1/' | head -c 40)"
  else
    bad "$m -> $(echo "$r" | head -c 120)"
  fi
done

echo "=== 4. dead model = clean 503 ==="
code=$(curl -s -o /dev/null -w "%{http_code}" -m 30 -X POST "$B/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-free","messages":[{"role":"user","content":"hi"}]}')
[ "$code" = "503" ] && ok "mimo-v2.5-free -> 503" || bad "mimo-v2.5-free -> $code (want 503)"

echo
echo "================ $pass passed, $fail failed ================"
[ "$fail" -eq 0 ]
