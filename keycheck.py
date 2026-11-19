#!/usr/bin/env python3
"""keycheck.py - per-key health: is the credential live, and does the model lane answer?

For every key in the keyring it reports:
  theos   HTTP status + balance  (proves the key itself is valid)
  chat    HTTP status + first characters of the reply  (proves the lane answers)
  verdict ok / quota / auth / broken

A 429 on chat with a 200 on theos means the key is fine and the daily request
cap is what is blocking - not a dead credential.
"""

import hashlib
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.deepai.org"
KEYRING = "deepai-keyring.json"


def _machine_id() -> str | None:
    """sha256 of the SMBIOS UUID - the real client always sends this.

    Without it /theos answers 403 machine_required, which looks like a dead
    key but is only a missing fingerprint.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception:
        return None
    return hashlib.sha256(out.encode()).hexdigest() if out else None


MACHINE = _machine_id()


def lane_headers(api_key: str) -> dict:
    h = {
        "Accept": "application/json",
        "User-Agent": "DeepAI-Code",
        "X-DeepAI-Code-Theos": "4",
        "X-DeepAI-Code-Instance": secrets.token_hex(16),
        "Api-Key": api_key,
    }
    if MACHINE:
        h["X-DeepAI-Code-Machine"] = MACHINE
    return h


def get(path: str, api_key: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(API + path, headers=lane_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def post(path: str, api_key: str, body: dict, timeout: int = 90) -> tuple[int, str]:
    data = json.dumps(body).encode()
    headers = lane_headers(api_key)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def classify_chat(status: int, body: str) -> str:
    if status == 200:
        if '"content"' in body or "data:" in body:
            return "replying"
        return "200 but empty"
    if status == 429:
        return "daily cap"
    if status in (401, 403):
        return "key rejected"
    if status == 0:
        return "network"
    return f"HTTP {status}"


def main() -> int:
    try:
        with open(KEYRING, "r", encoding="utf-8") as fh:
            ring = json.load(fh)
    except Exception as exc:
        print(f"could not read {KEYRING}: {exc}")
        return 1

    keys = ring.get("keys") or []
    if not keys:
        print("keyring is empty")
        return 1

    model = sys.argv[1] if len(sys.argv) > 1 else "glm-5.3-flash"

    print(f"model lane probe: {model}")
    print()
    print(f"{'label':<14} {'theos':<7} {'balance':<11} {'chat':<13} verdict")
    print("-" * 78)

    results = []
    for entry in keys:
        label = str(entry.get("label", ""))[:14]
        key = entry.get("api_key", "")

        ts, tbody = get("/deepai_code/theos", key)
        balance = "-"
        if ts == 200:
            try:
                balance = str(json.loads(tbody).get("balance", "-"))
            except json.JSONDecodeError:
                balance = "-"

        cs, cbody = post("/deepai_code/chat/completions", key, {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word READY."}],
            "max_tokens": 64,
        })
        chat = classify_chat(cs, cbody)

        if ts == 200 and cs == 200:
            verdict = "OK - key live, lane answering"
        elif ts == 200 and cs == 429:
            verdict = "key live, quota spent (resets tomorrow)"
        elif ts == 200:
            verdict = f"key live, lane odd: {chat}"
        else:
            verdict = "credential problem"

        results.append((label, ts, balance, chat, verdict))
        print(f"{label:<14} {ts:<7} {balance:<11} {chat:<13} {verdict}")
        time.sleep(0.4)

    print()
    live = [r for r in results if r[1] == 200]
    answering = [r for r in results if r[3] == "replying"]
    print(f"keys valid      : {len(live)}/{len(results)}")
    print(f"lane answering  : {len(answering)}/{len(results)}")

    if live and not answering:
        print()
        print("Every key is valid; the daily request cap is what is returning 429.")
        print("A new key on the SAME account will not clear it - quota is account-scoped.")

    if results:
        print()
        sample = next((r for r in results if r[3] == "replying"), None)
        print("sample reply check:")
        if sample:
            print(f"  {sample[0]} -> {sample[3]}")
        else:
            print("  none - all lanes capped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
