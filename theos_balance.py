"""Check the account's Theos balance before any premier-lane probe.

POSTs to the lane's /theos endpoint the same way the desktop app does. Prints
only the balance fields -- never the key or any token string.
"""
import json
import os
import urllib.error
import urllib.request

ring = json.load(open("deepai-keyring.json", encoding="utf-8"))
key = ring["keys"][0]["api_key"]

lane = {}
if os.path.exists(".deepai-lane.json"):
    try:
        lane = json.load(open(".deepai-lane.json", encoding="utf-8"))
    except Exception:
        lane = {}

headers = {
    "Api-Key": key,
    "X-DeepAI-Code-Theos": "4",
    "X-DeepAI-Code-Instance": os.urandom(16).hex(),
    "User-Agent": "DeepAI-Code",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
if lane.get("machine"):
    headers["X-DeepAI-Code-Machine"] = lane["machine"]

for method, body in (("POST", b"{}"), ("GET", None)):
    req = urllib.request.Request("https://api.deepai.org/deepai_code/theos",
                                 data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read()[:400].decode("utf-8", "replace")
            print(f"{method} {resp.status}: {text}")
            break
    except urllib.error.HTTPError as exc:
        print(f"{method} HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}")
    except Exception as exc:
        print(f"{method} ERR {type(exc).__name__}: {exc}")
