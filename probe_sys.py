"""Does truncation spare the system message?

The earlier canary probe put its markers in user turns, so it proved the front
of the *conversation* is dropped but said nothing about the system message.
That difference decides whether a long thread merely loses old turns (an agent
that still works) or loses its instructions entirely (an agent that goes feral).

Four canaries: SYSTEM, HEAD (first user turn), MID, TAIL.
"""
import argparse
import json
import os
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
FILLER = ("the quick brown fox jumps over the lazy dog while the river carries "
          "leaves past the old stone bridge and the wind moves through grass ")

SYSTEM, HEAD, MID, TAIL = "ALMOND", "KUMQUAT", "MARMALADE", "PERSIMMON"


def build(target_tokens):
    msgs = [{"role": "system",
             "content": f"You are a terse assistant. Your system codeword is {SYSTEM}. "
                        "Never forget it."}]
    msgs.append({"role": "user", "content": f"Fact one. HEAD={HEAD}. Ack with OK."})
    msgs.append({"role": "assistant", "content": "OK"})
    pairs = max(1, (target_tokens * 4) // (len(FILLER) * 2))
    half = pairs // 2
    for i in range(pairs):
        if i == half:
            msgs.append({"role": "user", "content": f"Fact two. MID={MID}. Ack with OK."})
            msgs.append({"role": "assistant", "content": "OK"})
        msgs.append({"role": "user", "content": f"note {i}: {FILLER}"})
        msgs.append({"role": "assistant", "content": f"noted {i}"})
    msgs.append({"role": "user",
                 "content": f"Fact three. TAIL={TAIL}. Now name all four codewords as "
                            "SYSTEM=... HEAD=... MID=... TAIL=... . Use MISSING for any "
                            "you never saw."})
    return msgs


def ask(model, msgs):
    t0 = time.time()
    r = requests.post(URL, headers=HEADERS, timeout=900, json={
        "model": model, "stream": False, "max_tokens": 1500, "messages": msgs})
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return {"status": r.status_code, "text": str(r.text)[:140], "usage": {},
                "finish": None, "ms": ms}
    d = r.json()
    choice = (d.get("choices") or [{}])[0]
    return {"status": 200, "text": (choice.get("message") or {}).get("content") or "",
            "usage": d.get("usage") or {}, "finish": choice.get("finish_reason"),
            "ms": ms}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="glm-5.3-flash")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("sizes", nargs="*", type=int, default=[60000, 120000, 200000])
    a = ap.parse_args()

    print(f"model={a.model} runs={a.runs}")
    print(f"{'req_tok':>8}{'prompt_tok':>11}{'chars':>7}{'SYSTEM':>8}{'HEAD':>7}"
          f"{'MID':>6}{'TAIL':>7}{'finish':>10}{'verdict':>10}")
    for size in a.sizes:
        for _ in range(a.runs):
            msgs = build(size)
            res = ask(a.model, msgs)
            up = res["text"].upper()
            # An empty answer proves nothing: every marker looks "LOST" when
            # there is no text to search. Only rows that actually answered count.
            valid = res["status"] == 200 and len(res["text"].strip()) > 0
            marks = ("OK" if SYSTEM in up else "LOST",
                     "OK" if HEAD in up else "LOST",
                     "OK" if MID in up else "LOST",
                     "OK" if TAIL in up else "LOST")
            print(f"{size:>8}{str(res['usage'].get('prompt_tokens')):>11}"
                  f"{len(res['text']):>7}{marks[0]:>8}{marks[1]:>7}{marks[2]:>6}"
                  f"{marks[3]:>7}{str(res['finish']):>10}"
                  f"{('valid' if valid else 'INVALID'):>10}")
            if not valid:
                print("    empty/unusable answer:", res["text"][:80] not in ("",) and res["text"][:80])
            time.sleep(1)


if __name__ == "__main__":
    main()
