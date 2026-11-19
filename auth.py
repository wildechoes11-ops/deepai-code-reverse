#!/usr/bin/env python3
"""auth.py - sign in to DeepAI Code and extract the Api-Key.

The desktop app's whole sign-in is four steps, and nothing here is a trick: it
is the same flow the official client performs, driven from a terminal.

    1. GET   https://api.deepai.org/deepai_code/desktop_auth/?state=<hex>&port=<P>
    2. the browser lands on deepai.org, and you sign up or log in there
    3. it 302s back to   http://127.0.0.1:<P>/auth-callback?code=<code>&state=<hex>
    4. POST  https://api.deepai.org/deepai_code/desktop_auth/exchange/ {"code": ...}
       ->    {"api_key": "...", "username": "...", "is_pro": bool}

Step 4 *is* the mint: the web signup never shows a key, the exchange response
carries it. Everything below funnels into that.

The extracted key is written to a keyring file in the format the proxy reads:

    {"keys": [{"label": "acct", "api_key": "...", "username": "...",
               "is_pro": false, "added": "2026-11-19T12:00:00Z"}]}

Modes
-----
  signin     full flow: open the sign-in link, catch the loopback code,
             exchange it, save the key, verify it against /theos   (default)
  link       print the sign-in URL and exit -- no listener, no browser
  exchange   exchange a ?code= you already captured
  from-app   extract the key straight off a signed-in machine, no network
  list       show the keyring
  verify     hit /theos with every stored key and report status

Examples
--------
    python auth.py                          # sign in, save, verify
    python auth.py --link                   # just the URL to paste around
    python auth.py --exchange <code>        # you already have a code
    python auth.py --from-app               # read the local app's stored key
    python auth.py --label acct2 --out deepai-keyring.json
    python auth.py list

Notes
-----
* The desktop app stores its key as plain JSON, no OS keyring, no encryption:
      <home>/userdata/secrets/deepai-account.bin
      {"apiKey": "...", "username": "...", "isPro": false}
  That is what --from-app reads. `<home>` is %USERPROFILE%\\.deepai-code by
  default, %APPDATA%\\deepai-code-v2 for the v2 profile.
* --from-app and signin are independent routes to the same credential. Neither
  needs admin rights or injection.
* Stdlib only. No Playwright, no browser automation: the browser is the real
  one you already have, so a captcha or a verification step is a click, not a
  failure mode.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API_ORIGIN = "https://api.deepai.org"
SIGN_IN_URL = f"{API_ORIGIN}/deepai_code/desktop_auth/"
EXCHANGE_URL = f"{API_ORIGIN}/deepai_code/desktop_auth/exchange/"
THEOS_URL = f"{API_ORIGIN}/deepai_code/theos"

DEFAULT_KEYRING = "deepai-keyring.json"

PAGE_OK = """<!doctype html>
<meta charset="utf-8">
<title>DeepAI Code - signed in</title>
<style>
  body { font: 15px/1.6 system-ui, sans-serif; margin: 12vh auto; max-width: 34rem;
         text-align: center; color: #111; }
  .ok { color: #0a7d33; }
</style>
<h2 class="ok">Signed in to DeepAI Code</h2>
<p>Return to the terminal. Your key was printed there.</p>
"""

PAGE_BAD = """<!doctype html>
<meta charset="utf-8">
<title>DeepAI Code - failed</title>
<h2>Sign-in did not complete</h2>
<p>{reason}</p>
"""


# --- keyring ------------------------------------------------------------- #

def load_keyring(path: str) -> dict:
    if not os.path.isfile(path):
        return {"keys": []}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return {"keys": []}
    if not isinstance(doc, dict):
        return {"keys": []}
    if not isinstance(doc.get("keys"), list):
        doc["keys"] = []
    return doc


def chrome(path: str) -> None:
    """Tighten permissions as far as the platform allows. Best effort."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            if user:
                os.system(f'icacls "{path}" /inheritance:r /grant:r "{user}:F" '
                          '>nul 2>&1')
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def save_key(path: str, label: str, key: str, username: str,
             is_pro: bool) -> None:
    """Merge one key into the keyring, replacing any entry with the same label."""
    doc = load_keyring(path)
    entry = {
        "label": label,
        "api_key": key,
        "username": username,
        "is_pro": bool(is_pro),
        "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    keys = [k for k in doc["keys"]
            if isinstance(k, dict) and k.get("label") != label]
    keys.append(entry)
    doc["keys"] = keys
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    chrome(path)


# --- network ------------------------------------------------------------- #

def free_port(preferred: int | None = None) -> int:
    candidates = ([preferred] if preferred else []) + list(range(8765, 8865))
    for candidate in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError("no free loopback port in 8765-8864")


def sign_in_url(port: int, state: str) -> str:
    return f"{SIGN_IN_URL}?{urllib.parse.urlencode({'state': state, 'port': port})}"


def exchange(code: str) -> dict:
    """POST the code, get the key. This is the whole mint."""
    body = json.dumps({"code": code}).encode()
    req = urllib.request.Request(
        EXCHANGE_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DeepAI-Code",
            "X-DeepAI-Code-Theos": "4",
            "X-DeepAI-Code-Instance": secrets.token_hex(16),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return {"status": resp.status,
                    "body": json.loads(resp.read().decode())}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return {"status": exc.code, "body": json.loads(raw)}
        except json.JSONDecodeError:
            return {"status": exc.code, "body": {"raw": raw[:400]}}
    except Exception as exc:
        return {"status": 0, "body": {"error": f"{type(exc).__name__}: {exc}"}}


def verify(key: str) -> dict:
    """Cheapest liveness check: /theos answers 200 for a key the lane accepts."""
    req = urllib.request.Request(THEOS_URL, headers={
        "Api-Key": key,
        "Accept": "application/json",
        "User-Agent": "DeepAI-Code",
        "X-DeepAI-Code-Theos": "4",
        "X-DeepAI-Code-Instance": secrets.token_hex(16),
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return {"status": resp.status,
                    "body": json.loads(resp.read().decode() or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return {"status": exc.code, "body": json.loads(raw)}
        except json.JSONDecodeError:
            return {"status": exc.code, "body": {"raw": raw[:300]}}
    except Exception as exc:
        return {"status": 0, "body": {"error": f"{type(exc).__name__}: {exc}"}}


# --- loopback catcher ---------------------------------------------------- #

class QuietServer(http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return  # the browser hung up; not worth a traceback
        super().handle_error(request, client_address)


class CallbackCatcher:
    """One-shot loopback listener for /auth-callback?code=&state=."""

    def __init__(self, port: int, expected_state: str):
        self.port = port
        self.state = expected_state
        self.code: str | None = None
        self.error: str | None = None
        self._done = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # Close after every response: a browser holds keep-alive open and
            # socketserver would then log a spurious reset.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: str) -> None:
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/auth-callback":
                    self._send(404, PAGE_BAD.format(reason="Not found."))
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                state = (qs.get("state") or [""])[0]
                if not code or len(code) > 4096:
                    outer.error = "missing or oversized code"
                    self._send(400, PAGE_BAD.format(reason="No code in callback."))
                    outer._done.set()
                    return
                if not secrets.compare_digest(state, outer.state):
                    # The state ties this callback to the request that started
                    # it; a mismatch means a stray or replayed link.
                    outer.error = "state mismatch"
                    self._send(400, PAGE_BAD.format(reason="State mismatch."))
                    outer._done.set()
                    return
                outer.code = code
                outer.error = None   # a stray bad hit must not poison a good one
                self._send(200, PAGE_OK)
                outer._done.set()

        self._server = QuietServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def wait(self, timeout: float = 300) -> bool:
        return self._done.wait(timeout)

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        return False


# --- the app's own store ------------------------------------------------- #

def app_homes() -> list[str]:
    homes = []
    env = os.environ.get("DEEPAI_CODE_HOME")
    if env:
        homes.append(env)
    homes.append(os.path.join(os.path.expanduser("~"), ".deepai-code"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        homes.append(os.path.join(appdata, "deepai-code-v2"))
    # de-duplicate, keep order
    seen = set()
    return [h for h in homes if not (h in seen or seen.add(h))]


def from_app() -> tuple[str, str, bool] | None:
    """Read the key the desktop app already stored. No network, no admin."""
    for home in app_homes():
        for candidate in (
            os.path.join(home, "userdata", "secrets", "deepai-account.bin"),
            os.path.join(home, "userdata", "secrets", "deepai-account.json"),
        ):
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, encoding="utf-8", errors="replace") as fh:
                    doc = json.load(fh)
            except Exception:
                continue
            key = doc.get("apiKey") or doc.get("api_key")
            if key:
                return (key, doc.get("username") or "", bool(doc.get("isPro")))
    return None


# --- reporting ----------------------------------------------------------- #

def describe(result: dict) -> str:
    status = result.get("status")
    body = result.get("body") or {}
    if status == 200 and body.get("api_key"):
        return "ok"
    if status == 200:
        return "answered 200 but carried no api_key"
    if status == 401 or status == 403:
        return f"rejected by the lane ({body.get('error', {}) or status})"
    if status == 429:
        return "rate limited -- the key is fine, the daily cap is the blocker"
    if status == 0:
        return f"network error: {body.get('error', 'unknown')}"
    return f"HTTP {status}: {str(body)[:160]}"


def print_key(key: str, username: str, is_pro: bool) -> None:
    print()
    print("  api_key  :", key)
    print("  username :", username or "(not reported)")
    print("  pro      :", "yes" if is_pro else "no")
    print()


# --- modes --------------------------------------------------------------- #

def do_signin(args) -> int:
    port = free_port(args.port)
    state = secrets.token_hex(16)
    url = sign_in_url(port, state)

    print(f"  listening 127.0.0.1:{port}")
    print()
    print("  Sign in here:")
    print("   ", url)
    print()
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    with CallbackCatcher(port, state) as catcher:
        if not catcher.wait(args.timeout):
            print(f"  timed out after {args.timeout}s -- no callback arrived")
            return 1

    if catcher.error or not catcher.code:
        print(f"  callback failed: {catcher.error or 'no code'}")
        return 1

    print("  code captured, exchanging ...")
    result = exchange(catcher.code)
    if result.get("status") != 200 or not (result.get("body") or {}).get("api_key"):
        print("  exchange failed:", describe(result))
        return 1

    body = result["body"]
    key = body["api_key"]
    username = body.get("username") or ""
    is_pro = bool(body.get("is_pro"))

    print_key(key, username, is_pro)
    save_key(args.out, args.label, key, username, is_pro)
    print(f"  saved to {os.path.abspath(args.out)} [{args.label}]")

    check = verify(key)
    print("  verify /theos:", check.get("status"),
          str(check.get("body"))[:120])
    return 0


def do_link(args) -> int:
    port = free_port(args.port)
    state = secrets.token_hex(16)
    url = sign_in_url(port, state)
    print(url)
    print()
    print(f"# state={state} port={port}")
    print("# the browser must be able to reach 127.0.0.1:%d to complete the" % port)
    print("# callback; with no listener there, copy ?code= off the failed redirect")
    print("# and run:  python auth.py --exchange <code>")
    return 0


def do_exchange(args) -> int:
    code = args.exchange.strip()
    if code.startswith("http"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        code = (qs.get("code") or [""])[0]
    if not code:
        print("  no code found in the argument")
        return 1
    print("  exchanging ...")
    result = exchange(code)
    if result.get("status") != 200 or not (result.get("body") or {}).get("api_key"):
        print("  exchange failed:", describe(result))
        return 1
    body = result["body"]
    key = body["api_key"]
    username = body.get("username") or ""
    is_pro = bool(body.get("is_pro"))
    print_key(key, username, is_pro)
    save_key(args.out, args.label, key, username, is_pro)
    print(f"  saved to {os.path.abspath(args.out)} [{args.label}]")
    print("  verify /theos:", verify(key).get("status"))
    return 0


def do_from_app(args) -> int:
    found = from_app()
    if not found:
        print("  no stored account found in:")
        for home in app_homes():
            print("   ", os.path.join(home, "userdata", "secrets",
                                      "deepai-account.bin"))
        print()
        print("  sign in once with the desktop app, or use: python auth.py")
        return 1
    key, username, is_pro = found
    print_key(key, username, is_pro)
    save_key(args.out, args.label, key, username, is_pro)
    print(f"  saved to {os.path.abspath(args.out)} [{args.label}]")
    check = verify(key)
    print("  verify /theos:", check.get("status"),
          str(check.get("body"))[:120])
    return 0


def do_list(args) -> int:
    doc = load_keyring(args.out)
    keys = [k for k in doc.get("keys", []) if isinstance(k, dict)]
    if not keys:
        print(f"  {args.out}: no keys")
        return 0
    print(f"  {args.out}: {len(keys)} key(s)")
    for entry in keys:
        key = str(entry.get("api_key") or "")
        masked = (key[:8] + "..." + key[-4:]) if len(key) > 16 else "(short)"
        print(f"    {entry.get('label', '?'):<12} {masked:<24} "
              f"user={entry.get('username', '?'):<16} "
              f"pro={'yes' if entry.get('is_pro') else 'no'}")
    return 0


def do_verify(args) -> int:
    doc = load_keyring(args.out)
    keys = [k for k in doc.get("keys", []) if isinstance(k, dict)]
    if not keys:
        print(f"  {args.out}: no keys")
        return 1
    bad = 0
    for entry in keys:
        key = entry.get("api_key") or ""
        label = entry.get("label", "?")
        if not key:
            print(f"    {label:<12} no key stored")
            bad += 1
            continue
        result = verify(key)
        status = result.get("status")
        print(f"    {label:<12} HTTP {status:<5} {str(result.get('body'))[:110]}")
        if status != 200:
            bad += 1
    return 0 if bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sign in to DeepAI Code and extract the Api-Key.",
        epilog="modes: signin (default), --link, --exchange CODE, --from-app, "
               "list, verify")
    ap.add_argument("--out", default=DEFAULT_KEYRING,
                    help="keyring file to write (default: deepai-keyring.json)")
    ap.add_argument("--label", default="default",
                    help="keyring entry label (default: default)")
    ap.add_argument("--port", type=int, default=None,
                    help="preferred loopback port for the callback")
    ap.add_argument("--timeout", type=float, default=300,
                    help="seconds to wait for the callback (default: 300)")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the link instead of opening a browser")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--link", action="store_true",
                      help="print the sign-in URL and exit")
    mode.add_argument("--exchange", metavar="CODE",
                      help="exchange a captured ?code= (or a full callback URL)")
    mode.add_argument("--from-app", action="store_true",
                      help="extract the key the desktop app already stored")
    mode.add_argument("--list", action="store_true", help="show the keyring")
    mode.add_argument("--verify", action="store_true",
                      help="check every stored key against /theos")
    ap.add_argument("command", nargs="?", choices=["signin", "list", "verify"],
                    help="positional mode alias")

    args = ap.parse_args(argv)

    if args.command == "list":
        args.list = True
    elif args.command == "verify":
        args.verify = True

    if args.link:
        return do_link(args)
    if args.exchange:
        return do_exchange(args)
    if args.from_app:
        return do_from_app(args)
    if args.list:
        return do_list(args)
    if args.verify:
        return do_verify(args)
    return do_signin(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  interrupted")
        sys.exit(130)
