#!/usr/bin/env python3
"""deepai-model-filter.py - local passthrough proxy that trims the model catalog.

DeepAI Code asks https://api.deepai.org/deepai_code/models for its picker and
renders whatever comes back. This proxy sits on 127.0.0.1, forwards everything
untouched, and answers that ONE path with a filtered catalog.

The app supports it natively - no asar patching, no binary edits:

    T3CODE_DEEPAI_LOCAL_TEST=true
    T3CODE_DEEPAI_DEV_API_ORIGIN=http://127.0.0.1:<port>

`resolveDeepAIApiOrigin` accepts that pair when the origin is a canonical
loopback URL, and every account call plus the model lane then goes to it
(see src/deepai/api.ts). Redirects are rejected by the client, so nothing is
followed here either.

Only 0-Theos models are kept - the free set DeepAI's own cards mark with
`premier: false` and `theosPerRequest: "0"`, so a new free model appears in the
picker by itself. The ids below are a fallback for older catalogs that carry
neither field:

    glm-5.3-flash  deepseek-v4.1-flash  mimo-2.6-flash  qwen-3.8-flash
    minimax-m3.1-flash  solar-mini-4  gpt-6-luna  mimo-2.6-pro
    glyph-cluster  deepai-cyber

Usage:
    python deepai-model-filter.py                  # port 8788, filtered
    python deepai-model-filter.py --port 8899
    python deepai-model-filter.py --model glm-5.3-flash
    python deepai-model-filter.py --all            # pass everything through

Then start the app (see launch-deepai-filtered.ps1).
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import socket
import sys
import urllib.parse

UPSTREAM = "api.deepai.org"
MODELS_PATH = "/deepai_code/models"

FREE_MODELS = [
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

# Hop-by-hop headers must not be forwarded in either direction.
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Pinned ids from --model, or None to auto-detect 0-Theos models from the
# upstream cards themselves (premier=false AND theosPerRequest=0). FREE_MODELS
# is only the fallback for catalogs that carry neither field.
PINNED: set[str] | None = None


def is_free_model(m) -> bool:
    if not isinstance(m, dict) or not m.get("id"):
        return False
    if bool(m.get("premier")):
        return False
    theos = m.get("theosPerRequest")
    if theos is not None:
        try:
            return float(str(theos).strip()) == 0.0
        except (TypeError, ValueError):
            return False
    if "premier" in m:
        return True
    return m["id"] in FREE_MODELS


def filter_models(payload: bytes) -> tuple[bytes, int, int]:
    """Return (new body, kept, total). Non-JSON passes through untouched."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload, 0, 0
    models = data.get("models")
    if not isinstance(models, list):
        return payload, 0, 0
    total = len(models)
    if PINNED is not None:
        kept = [m for m in models
                if isinstance(m, dict) and m.get("id") in PINNED]
    else:
        kept = [m for m in models if is_free_model(m)]
    if not kept:
        # Never hand the app an empty picker: keep the upstream list.
        return payload, total, total
    data["models"] = kept
    return json.dumps(data).encode("utf-8"), len(kept), total


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = UPSTREAM
    passthrough = False

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    # -- plumbing ---------------------------------------------------------- #
    def _body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if length:
            try:
                n = int(length)
            except ValueError:
                return b""
            return self.rfile.read(n) if n > 0 else b""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return None

    def _connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(self.upstream, timeout=300)

    def _forward(self) -> None:
        body = self._body()
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP}
        conn = self._connect()
        try:
            conn.putrequest(self.command, self.path, skip_host=True,
                            skip_accept_encoding=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Host", self.upstream)
            conn.endheaders(body if body is not None else None)
            resp = conn.getresponse()

            ctype = (resp.getheader("Content-Type") or "").lower()
            streaming = ctype.startswith("text/event-stream")
            raw = resp.read() if not streaming else None

            if (resp.status == 200 and self.command == "GET"
                    and urllib.parse.urlparse(self.path).path == MODELS_PATH
                    and not self.passthrough and raw is not None):
                new_body, kept, total = filter_models(raw)
                if kept and kept != total:
                    sys.stderr.write(
                        f"  models trimmed {total} -> {kept}\n")
                raw = new_body

            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in HOP:
                    continue
                self.send_header(key, value)

            if streaming:
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    chunk = resp.read1(8192) if hasattr(resp, "read1") \
                        else resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return

            self.send_header("Content-Length", str(len(raw or b"")))
            self.end_headers()
            if raw:
                self.wfile.write(raw)
        except (http.client.HTTPException, socket.timeout, OSError) as exc:
            try:
                self.send_error(502, f"upstream: {type(exc).__name__}")
            except OSError:
                pass
        finally:
            conn.close()

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_HEAD = _forward


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return  # the app dropped the socket; not an error
        super().handle_error(request, client_address)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", action="append", metavar="ID",
                    help="allow this id (repeatable); replaces the default set")
    ap.add_argument("--all", action="store_true", help="do not filter")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    global PINNED
    if args.model:
        PINNED = set(args.model)

    Proxy.passthrough = args.all
    server = Server((args.host, args.port), Proxy)
    server.verbose = args.verbose

    if args.all:
        keep = "everything (passthrough, includes premier)"
    elif PINNED is not None:
        keep = f"{len(PINNED)} pinned ids"
    else:
        keep = "auto-detected 0-Theos models"
    print(f"deepai model filter  http://{args.host}:{args.port}")
    print(f"  upstream  https://{UPSTREAM}")
    print(f"  keeping   {keep}")
    if PINNED is not None:
        for mid in sorted(PINNED):
            print(f"            {mid}")
    elif not args.all:
        print("            resolved from the live catalog on first request")
    print("\n  set these before launching DeepAI Code:")
    print("    T3CODE_DEEPAI_LOCAL_TEST=true")
    print(f"    T3CODE_DEEPAI_DEV_API_ORIGIN=http://{args.host}:{args.port}")
    print("\n  ctrl-c to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
