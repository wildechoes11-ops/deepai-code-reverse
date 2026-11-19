"""How much of the prompt does the DeepAI chat lane actually accept?

Plants a codeword at the start (head), one mid-way (middle) and one inside the
final question (tail), then asks for all three. Reports what upstream says it
counted (usage.prompt_tokens), finish_reason, and which codewords survived.

If prompt_tokens plateaus below what we sent and HEAD goes MISSING while TAIL
still works, upstream is keeping only the last N tokens -- the exact mechanism
behind "the agent forgot the task" in a long session.

Run:
    python probe_context.py                      # default model + sizes
    python probe_context.py --model muse-ember 32000 48000 64000
"""
import argparse
import json
import os
import time

import requests

ring = json.load(open("deepai-keyring.json", encoding="utf-8"))
lane = json.load(open(".deepai-lane.json", encoding="utf-8"))
KEY = ring["keys"][0]["api_key"]

BASE = {
    "Api-Key": KEY,
    "X-DeepAI-Code-Theos": "4",
    "X-DeepAI-Code-Instance": os.urandom(16).hex(),
    "User-Agent": "DeepAI-Code",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
if lane.get("machine"):
    BASE["X-DeepAI-Code-Machine"] = lane["machine"]

URL = "https://api.deepai.org/deepai_code/chat/completions"

FILLER = ("the quick brown fox jumps over the lazy dog while the river "
          "carries leaves past the old stone bridge and the wind moves "
          "through the tall grass near the water ")

HEAD = "KUMQUAT"
MID = "MARMALADE"
TAIL = "PERSIMMON"


def build(target_tokens: int):
    msgs = [
        {"role": "system", "content": "You are a terse assistant. Answer in one short line."},
        {"role": "user", "content": f"Fact one. HEAD={HEAD}. Acknowledge with OK."},
        {"role": "assistant", "content": "OK"},
    ]
    pairs = max(1, (target_tokens * 4) // (len(FILLER) * 2))
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"note {i}: {FILLER}"})
        msgs.append({"role": "assistant", "content": f"noted {i}"})
    msgs.append({"role": "user", "content": f"Fact two. MID={MID}. Ack with OK."})
    msgs.append({"role": "assistant", "content": "OK"})
    msgs.append({"role": "user",
                 "content": f"Fact three. TAIL={TAIL}. Now name all three codewords "
                            "as HEAD=... MID=... TAIL=... . Use MISSING for any "
                            "you never saw."})
    return msgs


def ask(model, msgs):
    t0 = time.time()
    r = requests.post(URL, headers=BASE, timeout=900, json={
        "model": model, "stream": False, "max_tokens": 60, "messages": msgs})
    ms = int((time.time() - t0) * 1000)
    try:
        d = r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text[:200], "usage": {},
                "finish": None, "reasoning": 0, "ms": ms}
    if r.status_code != 200:
        return {"status": r.status_code, "text": str(d.get("error"))[:200],
                "usage": {}, "finish": None, "reasoning": 0, "ms": ms}
    choice = (d.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return {"status": 200, "text": text.strip(), "usage": d.get("usage") or {},
            "finish": choice.get("finish_reason"), "reasoning": len(reasoning),
            "ms": ms}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("PROBE_MODEL", "glm-5.3-flash"))
    ap.add_argument("sizes", nargs="*", type=int,
                    default=[60000, 90000, 120000, 160000, 220000])
    a = ap.parse_args()

    print(f"model={a.model}\n")
    for size in a.sizes:
        msgs = build(size)
        chars = sum(len(str(m.get("content") or "")) for m in msgs)
        res = ask(a.model, msgs)
        sent_est = chars // 4
        usage = res["usage"]
        up = res["text"].upper()
        print(f"req~{size:>7}  sent_est_tok={sent_est:>7}  "
              f"upstream_prompt_tokens={usage.get('prompt_tokens')}  "
              f"completion={usage.get('completion_tokens')}  "
              f"finish={res['finish']}  status={res['status']}  {res['ms']}ms")
        print(f"   evidence: head={'OK' if HEAD in up else 'LOST'} "
              f"mid={'OK' if MID in up else 'LOST'} "
              f"tail={'OK' if TAIL in up else 'LOST'}  "
              f"reasoning_chars={res['reasoning']}")
        print(f"   reply={res['text'][:160]!r}\n")


if __name__ == "__main__":
    main()
