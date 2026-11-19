#!/usr/bin/env python3
"""distinct.py - behavioural distinctness probe across the 10 free ids.

Sends three discriminating prompts to every model and prints a compact
per-model signature so aliases (two ids hitting the same weights) stand out.
"""

import hashlib
import json
import urllib.error
import urllib.request

import os

BASE = os.environ.get("DEEPAI_PROXY_URL", "http://127.0.0.1:8788")
# The proxy accepts any bearer when DEEPAI_GATEWAY_TOKEN is unset. Set both
# sides from the environment so no token is hardcoded here.
KEY = os.environ.get("DEEPAI_GATEWAY_TOKEN", "local-dev-token")

MODELS = [
    "glm-5.3-flash",
    "deepseek-v4.1-flash",
    "mimo-2.6-flash",
    "qwen-3.8-flash",
    "minimax-m3.1-flash",
    "solar-mini-4",
    "gpt-6-luna",
    "mimo-2.6-pro",
    "glyph-cluster",
    "deepai-cyber",
]

PROBES = [
    ("arith", "What is 4871 * 93? Answer with the number only."),
    ("style", "Write one sentence describing rain. Max 12 words."),
    ("tokenize", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
]

MAX_TOKENS = 900


def call(model: str, prompt: str) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + KEY},
    )
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=180).read())
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        return {
            "ok": True,
            "echo": d.get("model"),
            "ptok": (d.get("usage") or {}).get("prompt_tokens"),
            "ctok": (d.get("usage") or {}).get("completion_tokens"),
            "text": (msg.get("content") or "").strip(),
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}"}


def main() -> None:
    results: dict[str, list[dict]] = {}
    for mid in MODELS:
        results[mid] = []
        for name, prompt in PROBES:
            r = call(mid, prompt)
            r["probe"] = name
            results[mid].append(r)
        print(f"done {mid}")

    print()
    print("=" * 100)
    print(f"{'id':<22} {'echo':<36} {'ptok':>5} {'ctok':>5} {'arith':>8}  sig")
    print("=" * 100)

    sigs = {}
    for mid in MODELS:
        rows = results[mid]
        echo = next((r.get("echo") for r in rows if r.get("ok")), "FAIL")
        ptok = [r.get("ptok") for r in rows if r.get("ok")]
        ctok = [r.get("ctok") for r in rows if r.get("ok")]
        arith_row = next(r for r in rows if r["probe"] == "arith")
        arith = "".join(c for c in (arith_row.get("text") or "") if c.isdigit())[:8] or "-"
        blob = "|".join((r.get("text") or "") for r in rows)
        sig = hashlib.sha1(blob.encode()).hexdigest()[:10]
        sigs.setdefault(sig, []).append(mid)
        print(f"{mid:<22} {str(echo):<36} {str(ptok[0] if ptok else '-'):>5} "
              f"{str(ctok[0] if ctok else '-'):>5} {arith:>8}  {sig}")

    print()
    print(f"expected arithmetic answer: 4871 * 93 = {4871 * 93}")
    print()
    dupes = {k: v for k, v in sigs.items() if len(v) > 1}
    if dupes:
        print("ALIASED (identical output sets):")
        for k, v in dupes.items():
            print(f"  {k}: {v}")
    else:
        print("no aliasing - every id produced a distinct output set")

    print()
    print("per-model style sentence:")
    for mid in MODELS:
        style = next((r for r in results[mid] if r["probe"] == "style"), {})
        t = (style.get("text") or "").replace("\n", " ")[:90]
        print(f"  {mid:<22} {t}")


if __name__ == "__main__":
    main()
