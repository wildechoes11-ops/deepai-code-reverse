"""Find the exact prompt-size cliff on the DeepAI chat lane.

Same lane, headers and auth the desktop app uses (api-key + Theos lane headers
+ machine id). Plants three codewords -- head, middle, tail -- and asks for all
three, so the answer is the botched prompt, not the botched model.

Output per size: sent estimate, upstream prompt_tokens, which codewords
survived, finish_reason. Repeat runs at one size show the two-backend split.
"""
import argparse
import json
import os
import statistics
import time

import requests

ring = json.load(open("deepai-keyring.json", encoding="utf-8"))
lane = json.load(open(".deepai-lane.json", encoding="utf-8"))
KEY = ring["keys"][0]["api_key"]

HEADERS = {
    "Api-Key": KEY,
    "X-DeepAI-Code-Theos": "4",
    "X-DeepAI-Code-Instance": os.urandom(16).hex(),
    "User-Agent": "DeepAI-Code",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
if lane.get("machine"):
    HEADERS["X-DeepAI-Code-Machine"] = lane["machine"]

URL = "https://api.deepai.org/deepai_code/chat/completions"

FILLER = ("the quick brown fox jumps over the lazy dog while the river "
          "carries leaves past the old stone bridge and the wind moves "
          "through the tall grass near the water ")

HEAD, MID, TAIL = "KUMQUAT", "MARMALADE", "PERSIMMON"


def build(target_tokens: int):
    msgs = [
        {"role": "system", "content": "You are a terse assistant. Answer in one short line."},
        {"role": "user", "content": f"Fact one. HEAD={HEAD}. Acknowledge with OK."},
        {"role": "assistant", "content": "OK"},
    ]
    pairs = max(1, (target_tokens * 4) // (len(FILLER) * 2))
    half = pairs // 2
    for i in range(pairs):
        if i == half:
            msgs.append({"role": "user", "content": f"Fact two. MID={MID}. Ack with OK."})
            msgs.append({"role": "assistant", "content": "OK"})
        msgs.append({"role": "user", "content": f"note {i}: {FILLER}"})
        msgs.append({"role": "assistant", "content": f"noted {i}"})
    msgs.append({"role": "user",
                 "content": f"Fact three. TAIL={TAIL}. Now name all three codewords "
                            "as HEAD=... MID=... TAIL=... . Use MISSING for any "
                            "you never saw."})
    return msgs


def ask(model, msgs):
    t0 = time.time()
    r = requests.post(URL, headers=HEADERS, timeout=900, json={
        "model": model, "stream": False, "max_tokens": 500, "messages": msgs})
    ms = int((time.time() - t0) * 1000)
    try:
        d = r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text[:120], "usage": {},
                "finish": None, "ms": ms}
    if r.status_code != 200:
        return {"status": r.status_code, "text": str(d.get("error"))[:120],
                "usage": {}, "finish": None, "ms": ms}
    choice = (d.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {"status": 200, "text": (msg.get("content") or "").strip(),
            "usage": d.get("usage") or {}, "finish": choice.get("finish_reason"),
            "ms": ms}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="glm-5.3-flash")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("sizes", nargs="*", type=int,
                    default=[28000, 30000, 31000, 32000, 33000, 34000,
                             36000, 38000, 40000, 42000, 44000])
    a = ap.parse_args()

    print(f"model={a.model} runs={a.runs}\n")
    print(f"{'req_tok':>8} {'sent_est':>9} {'prompt_tok':>10} {'head':>5} {'mid':>5} "
          f"{'tail':>5} {'finish':>10} {'status':>6} {'ms':>6}")
    for size in a.sizes:
        for _ in range(a.runs):
            msgs = build(size)
            chars = sum(len(str(m.get("content") or "")) for m in msgs)
            res = ask(a.model, msgs)
            up = res["text"].upper()
            u = res["usage"]
            print(f"{size:>8} {chars // 4:>9} {str(u.get('prompt_tokens')):>10} "
                  f"{'OK' if HEAD in up else 'LOST':>5} "
                  f"{'OK' if MID in up else 'LOST':>5} "
                  f"{'OK' if TAIL in up else 'LOST':>5} "
                  f"{str(res['finish']):>10} {res['status']:>6} {res['ms']:>6}")
            if res["status"] != 200 or str(res["finish"]) == "length":
                print(f"    note: {res['text'][:100]!r}")
            time.sleep(1)


if __name__ == "__main__":
    main()
