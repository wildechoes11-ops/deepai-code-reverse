"""Live calibration check: two identical long requests, same conversation id.

Run 1 trims against an uncalibrated estimator. The response's real prompt_tokens
updates the ratio, so run 2 should keep noticeably more history for the same
cap. Proves the guard learns the tokenizer instead of guessing it.
"""
import sys

import requests

FILLER = "the quick brown fox jumps over the lazy dog near the river bank "
PORT = sys.argv[1] if len(sys.argv) > 1 else "8798"
URL = f"http://127.0.0.1:{PORT}/v1/chat/completions"
HEADERS = {"X-Conversation-Id": "calib-2"}


def build(pairs):
    msgs = [{"role": "system",
             "content": "You are a terse assistant. When asked, reply with exactly "
                        "BENCH-OK and nothing else."}]
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"turn {i}: " + FILLER})
        msgs.append({"role": "assistant", "content": f"ack {i} " + FILLER})
    msgs.append({"role": "user", "content": "Reply now."})
    return msgs


for attempt in (1, 2):
    r = requests.post(URL, headers=HEADERS, timeout=900, json={
        "model": "glm-5.3-flash", "messages": build(1500),
        "max_tokens": 500, "stream": False})
    guard = r.headers.get("X-Context-Guard") or ""
    data = r.json()
    usage = data.get("usage") or {}
    reply = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    print(f"run{attempt} status={r.status_code} actual_prompt={usage.get('prompt_tokens')}")
    print(f"        guard={guard}")
    print(f"        reply={reply!r}")
