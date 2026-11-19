"""Does the DeepAI chat lane keep state per X-Conversation-Id?

Probe A: same conversation id on both turns, no history resent.
Probe B: no conversation id at all (control).
"""
import json
import os
import uuid

import requests

ring = json.load(open("deepai-keyring.json", encoding="utf-8"))
lane = json.load(open(".deepai-lane.json", encoding="utf-8"))
KEY = ring["keys"][0]["api_key"]
INST = os.urandom(16).hex()

BASE = {
    "Api-Key": KEY,
    "X-DeepAI-Code-Theos": "4",
    "X-DeepAI-Code-Instance": INST,
    "User-Agent": "DeepAI-Code",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
if lane.get("machine"):
    BASE["X-DeepAI-Code-Machine"] = lane["machine"]

URL = "https://api.deepai.org/deepai_code/chat/completions"


def ask(msgs, extra=None):
    h = dict(BASE)
    h.update(extra or {})
    r = requests.post(URL, headers=h, timeout=300, json={
        "model": "glm-5.3-flash", "stream": False, "max_tokens": 120,
        "messages": msgs})
    try:
        d = r.json()
        c = (d.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception:
        c = r.text[:150]
    return r.status_code, c


store = [{"role": "user", "content": "Remember the codeword TANGERINE. Reply OK only."}]
recall = [{"role": "user", "content": "What codeword did I ask you to remember? One word."}]

cid = "conv-" + uuid.uuid4().hex[:12]
print("A1 store  (id)", ask(store, {"X-Conversation-Id": cid}))
print("A2 recall (id)", ask(recall, {"X-Conversation-Id": cid}))
print("B1 store  (no id)", ask(store))
print("B2 recall (no id)", ask(recall))
