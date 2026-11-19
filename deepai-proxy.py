#!/usr/bin/env python3
"""deepai-proxy.py - OpenAI + Anthropic compatible gateway over DeepAI Code.

Sits on 127.0.0.1 and speaks three dialects at once:

  OpenAI      GET  /v1/models
              GET  /v1/models/{id}        (context window per model)
              POST /v1/chat/completions   (stream + non-stream)

  Anthropic   GET  /v1/models
              GET  /v1/models/{id}
              POST /v1/messages            (stream + non-stream)
              POST /v1/messages/count_tokens

  DeepAI      GET  /deepai_code/models
              POST /deepai_code/chat/completions
              POST /deepai_code/theos

Every account call carries the lane headers. The catalog is trimmed to the
0-Theos free set unless --all is passed.

Usage:
    python deepai-proxy.py                            # port 8788
    python deepai-proxy.py --port 8799 --all
    python deepai-proxy.py --model deepseek-v4.1-flash

Then point any client at http://127.0.0.1:8788/v1
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import http.client
import http.server
import json
import os
import socket
import sys
import time
import urllib.parse
import uuid

UPSTREAM = "api.deepai.org"
MODELS_PATH = "/deepai_code/models"
LANE_VERSION = "4"

# Seed list used only before the first catalog fetch, and as a fallback if the
# catalog is unreachable. In normal operation the allowlist is detected from
# the upstream cards themselves (premier=false AND theosPerRequest=0), so a new
# free model appears automatically and a newly premier one never slips in.
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

HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

EXPLICIT_MODELS: set[str] | None = None   # None = auto-detect 0-Theos models
PASSTHROUGH = False

# Set when the gateway is reachable from the internet (a ModelScope Studio, a
# tunnel, a VPS). Clients then have to present this token: any value works as
# the "API key" they already have to configure, so agents need no new knob.
# Empty means local-only mode and nothing is required.
GATEWAY_TOKEN = os.environ.get("DEEPAI_GATEWAY_TOKEN", "").strip()

# Extra values that also unlock the gateway. On a ModelScope Studio the ingress
# insists on its own token in Authorization and consumes that header, so
# listing it here lets a client configure one key instead of two.
EXTRA_TOKENS = {t.strip() for t in
                (os.environ.get("DEEPAI_EXTRA_TOKENS") or "").split(",")
                if t.strip()}

# catalog cache: /v1/models is polled by every client, and the upstream call
# carries the lane headers, so keep one copy for ten minutes
_MODEL_CACHE: dict = {"at": 0.0, "models": []}


# A private console never renders its keys to an anonymous visitor. Behind a
# proxy (the ModelScope preview) the plain "/" is this unlock form instead of a
# bare 404, so the Studio's App tab still shows something useful.
CONSOLE_GATE_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeepAI Gateway</title><style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#0b0e14;color:#e6edf7;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.card{width:min(420px,90vw);background:#121722;border:1px solid #222b3a;
 border-radius:14px;padding:28px}
h1{font-size:18px;margin:0 0 6px}
p{color:#8b9bb4;font-size:13px;margin:0 0 18px}
form{display:flex;gap:8px}
input{flex:1;min-width:0;background:#161c29;border:1px solid #222b3a;border-radius:8px;
 padding:9px 11px;color:#e6edf7;font:13px ui-monospace,Consolas,monospace}
input:focus{outline:none;border-color:#5b9dff}
button{background:#5b9dff;border:0;border-radius:8px;padding:9px 15px;
 color:#08101f;font-weight:600;cursor:pointer;font:inherit;font-size:13px}
.hint{margin:16px 0 0;font-size:11px;color:#5d6b80}
</style></head><body><div class="card">
<h1>DeepAI Gateway</h1>
<p>This console is private. Enter the gateway token to continue.</p>
<form method="get" action="/">
<input type="password" name="console" placeholder="gwt_..." autocomplete="off" autofocus>
<button type="submit">Unlock</button>
</form>
<p class="hint">Use the token from your dashboard link. No keys or account data
 are shown until it is entered.</p>
</div></body></html>"""


def _load_key() -> str | None:
    key = os.environ.get("DEEPAI_API_KEY")
    if key:
        return key
    # a keyring entry beats the app's bin so the proxy can run a separate account
    ring_path = (os.environ.get("DEEPAI_KEYRING")
                 or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "deepai-keyring.json"))
    wanted = os.environ.get("DEEPAI_KEY_LABEL")
    if os.path.isfile(ring_path):
        try:
            ring = json.loads(open(ring_path, encoding="utf-8").read())
            entries = ring.get("keys") or []
            if wanted:
                for e in entries:
                    if e.get("label") == wanted and e.get("api_key"):
                        return e["api_key"]
            elif entries and entries[0].get("api_key"):
                return entries[0]["api_key"]
        except (OSError, json.JSONDecodeError):
            pass
    home = os.path.join(os.path.expanduser("~"), ".deepai-code")
    p = os.path.join(home, "userdata", "secrets", "deepai-account.bin")
    if os.path.isfile(p):
        try:
            data = json.loads(open(p, encoding="utf-8").read())
            if isinstance(data.get("apiKey"), str):
                return data["apiKey"]
        except (OSError, json.JSONDecodeError):
            pass
    return None


API_KEY = _load_key()


def _request_key() -> str | None:
    """The key to send upstream: the dashboard's active pick, else the ring default."""
    try:
        import dashboard
        override = dashboard.active_key()
        if override:
            return override
        # nothing has run through the proxy yet, so pick the first healthy key
        # instead of sending no Api-Key at all (which upstream answers 403)
        for _label, key in dashboard.key_candidates():
            if key:
                return key
    except Exception:
        pass
    return API_KEY


def is_free_model(m) -> bool:
    """A model is free when it is not premier and costs 0 Theos per request."""
    if not isinstance(m, dict) or not m.get("id") or bool(m.get("premier")):
        return False
    theos = m.get("theosPerRequest")
    if theos is None:
        return True          # older catalogs only had the premier flag
    try:
        return float(str(theos).strip()) == 0.0
    except (TypeError, ValueError):
        return False


def free_ids() -> list[str]:
    """0-Theos ids from the live catalog, or the seed list before it loads."""
    if EXPLICIT_MODELS is not None:
        seed = [m for m in FREE_MODELS if m in EXPLICIT_MODELS]
        return seed + sorted(EXPLICIT_MODELS - set(seed))
    cards = _MODEL_CACHE.get("models") or []
    ids = [m["id"] for m in cards if is_free_model(m)]
    return ids or list(FREE_MODELS)


def model_allowed(mid: str) -> bool:
    if not mid:
        return False
    if EXPLICIT_MODELS is not None:
        return mid in EXPLICIT_MODELS
    cards = _MODEL_CACHE.get("models") or []
    if cards:
        return any(isinstance(m, dict) and m.get("id") == mid
                   and is_free_model(m) for m in cards)
    return mid in FREE_MODELS


def first_allowed_model() -> str:
    ids = free_ids()
    return ids[0] if ids else FREE_MODELS[0]


def filter_models(payload: bytes) -> bytes:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    models = data.get("models")
    if not isinstance(models, list):
        return payload
    if EXPLICIT_MODELS is not None:
        kept = [m for m in models
                if isinstance(m, dict) and m.get("id") in EXPLICIT_MODELS]
    else:
        kept = [m for m in models if is_free_model(m)]
    if not kept:
        # Never hand back the raw catalog here: that would expose premier
        # models to every client. An empty list fails closed and gets logged.
        print("[models] no 0-Theos models in the upstream catalog",
              file=sys.stderr, flush=True)
        data["models"] = []
        return json.dumps(data).encode("utf-8")
    data["models"] = kept
    return json.dumps(data).encode("utf-8")


def machine_id() -> str | None:
    import hashlib, subprocess
    uuid_val = ""
    try:
        uuid_val = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip().lower()
    except Exception:
        pass
    placeholders = {
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "03000200-0400-0500-0006-000700080009",
    }
    if uuid_val and uuid_val not in placeholders:
        return hashlib.sha256(uuid_val.encode()).hexdigest()
    try:
        out = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v",
             "MachineGuid"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            if "MachineGuid" in line:
                guid = line.split()[-1].strip().lower()
                if guid:
                    return hashlib.sha256(guid.encode()).hexdigest()
    except Exception:
        pass
    return None


MACHINE = (os.environ.get("DEEPAI_MACHINE") or "").strip().lower() or machine_id()


# ---------------------------------------------------------------------------
# Anthropic <-> OpenAI translation
# ---------------------------------------------------------------------------
def anthropic_to_openai(body: dict) -> dict:
    """Anthropic /v1/messages body -> DeepAI (OpenAI) chat body."""
    messages = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(b.get("text", "") for b in system
                       if isinstance(b, dict))
        if text:
            messages.append({"role": "system", "content": text})

    for m in body.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            # flatten content blocks into text (tool_use blocks dropped for now)
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(str(block.get("content", "")))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)
        messages.append({"role": role, "content": content})

    out = {
        "model": body.get("model", first_allowed_model()),
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    return out


def openai_to_anthropic(resp_body: dict, model: str) -> dict:
    """DeepAI (OpenAI) response -> Anthropic /v1/messages response."""
    choices = resp_body.get("choices") or [{}]
    ch = choices[0]
    msg = ch.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        content = ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    blocks = []
    if isinstance(reasoning, str) and reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning})
    blocks.append({"type": "text", "text": content})

    finish = ch.get("finish_reason")
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "end_turn",
        "tool_calls": "tool_use",
    }.get(finish, "end_turn")

    usage = resp_body.get("usage") or {}
    return {
        "id": resp_body.get("id", "msg_" + uuid.uuid4().hex[:24]),
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": resp_body.get("model", model),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --- context guard ------------------------------------------------------- #
#
# DeepAI's catalog prints contextWindow 320000 per model. The serving tier does
# not honour it. Measured with head/middle/tail canaries on glm-5.3-flash, two
# runs per size, `max_tokens` high enough that reasoning cannot eat the answer:
#
#   prompt_tokens  37961  all three   safe
#   prompt_tokens  40766  all three   safe
#   prompt_tokens  42141  all three   safe
#   prompt_tokens  43516  all three   safe
#   prompt_tokens  43813  HEAD LOST   lossy  (2/2 runs)
#   prompt_tokens  43868  HEAD LOST   lossy  (2/2 runs)
#   prompt_tokens  44474  all lost    lossy  (finish_reason "length", empty body)
#   prompt_tokens  46059  HEAD LOST   lossy
#   prompt_tokens  48613  HEAD LOST   lossy
#
# The knee sits between 43,516 and 43,813 prompt tokens. Past it, upstream keeps
# the TAIL and discards the FRONT -- HTTP 200, no error, and `prompt_tokens`
# reports the reduced count it actually ingested, so a usage-driven compaction
# meter never notices it is measuring the amputation rather than the prompt. A
# client trusting 320000 compacts near 300k and has lost its system prompt many
# times over by then. 40,000 keeps ~3.5k of margin under the knee.
#
# Two things live here. First, an honest ceiling the catalog can advertise, so
# clients compact early enough to be correct. Second, the thing the desktop app
# gets for free and a stateless lane cannot: the app stores the whole transcript
# locally and re-sends it every turn, so it always remembers. A client that
# sends only a tail (a resumed session, a trimmed tool loop, a retry) forgets.
# The proxy keeps a bounded transcript per conversation id and folds the missing
# head back in before forwarding.

def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


CONTEXT_CAP = _env_int("DEEPAI_CONTEXT_CAP", 40000)   # 0 disables the guard
OUTPUT_CAP = _env_int("DEEPAI_OUTPUT_CAP", 8192)      # advertised, also clamped
MEMORY_TURNS = _env_int("DEEPAI_MEMORY_TURNS", 600)
MEMORY_SESSIONS = _env_int("DEEPAI_MEMORY_SESSIONS", 64)
TRIM_NOTE = "[earlier turns trimmed to fit the model's usable context]"

# Conversation id -> ordered transcript. Bounded twice: by session count (LRU)
# and by turn count per session. Never persisted; the process is the store.
_MEMORY: "collections.OrderedDict[str, list]" = collections.OrderedDict()

# Conversation id -> (actual prompt_tokens / estimated tokens). No estimator
# beats the provider's own count, so every response recalibrates the next
# request: prose tokenizes near 5 chars/token, source code near 3.5, and a
# fixed divisor is wrong for one of them. Below 1.0 means the estimate was
# pessimistic and there is room to keep more history.
_RATIO: dict[str, float] = {}


def _msg_text(msg: dict) -> str:
    """Flatten a message's content to plain text for measuring."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
                elif block.get("type") == "image_url":
                    parts.append(" [image] ")
        return "".join(parts)
    return ""


def _msg_tokens(msg: dict) -> int:
    # Deliberately pessimistic. Prose tokenizes near 4 chars/token, but source
    # code and JSON sit closer to 3, and a tool payload closer still -- an
    # underestimate here means the guard under-trims and upstream cuts the head
    # instead, which is the exact failure this exists to prevent.
    text = _msg_text(msg)
    calls = msg.get("tool_calls")
    if isinstance(calls, list) and calls:
        text += json.dumps(calls, ensure_ascii=False)
        return len(text) // 3 + 16
    return len(text) // 3 + 16


def _conv_key(body: dict, headers) -> str | None:
    """Stable conversation key, in the order the common clients supply it."""
    for header in ("X-Conversation-Id", "X-Session-Affinity",
                   "X-Claude-Code-Session-Id", "X-Session-Id"):
        value = headers.get(header) if hasattr(headers, "get") else None
        if value:
            return "h:" + value.strip()[:128]
    cache_key = body.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key.strip():
        return "c:" + cache_key.strip()[:128]
    # No id from the client: fall back to the opening user turn, which is the
    # one thing that stays constant across compactions of the same thread.
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            seed = _msg_text(msg)[:400]
            if seed:
                return "s:" + hashlib.sha256(
                    (str(body.get("model")) + "\x00" + seed).encode("utf-8")
                ).hexdigest()[:32]
            break
    return None


def _fingerprint(msg: dict) -> str:
    return hashlib.sha256(
        (str(msg.get("role")) + "\x00" + _msg_text(msg)[:256]).encode("utf-8")
    ).hexdigest()[:16]


def _memory_store(key: str, messages: list, est_tokens: int = 0) -> None:
    if not key or not messages:
        return
    kept = [m for m in messages if isinstance(m, dict)
            and m.get("role") in ("system", "user", "assistant", "tool")]
    if len(kept) > MEMORY_TURNS:
        kept = kept[-MEMORY_TURNS:]
    _MEMORY[key] = kept
    _MEMORY.move_to_end(key)
    while len(_MEMORY) > MEMORY_SESSIONS:
        old, _ = _MEMORY.popitem(last=False)
        _RATIO.pop(old, None)


def note_prompt_usage(key: str | None, est_tokens: int, actual_tokens,
                      trimmed: int = 0) -> None:
    """Fold the provider's real prompt_tokens back into the estimator.

    `est_tokens` is the estimate of the prompt that was actually forwarded, so
    a trimmed sample is still valid: both sides of the quotient describe the
    same bytes. `trimmed` rides along only for the response header.
    """
    if not key or not est_tokens or not isinstance(actual_tokens, int):
        return
    if actual_tokens <= 0:
        return
    observed = actual_tokens / float(est_tokens)
    if not (0.2 <= observed <= 3.0):
        return
    previous = _RATIO.get(key)
    _RATIO[key] = observed if previous is None else (0.6 * previous + 0.4 * observed)


def _memory_restore(key: str, messages: list) -> tuple[list, int]:
    """Fold a remembered head back in when the client sent only a tail."""
    if not key:
        return messages, 0
    remembered = _MEMORY.get(key)
    if not remembered:
        return messages, 0
    if not messages:
        return messages, 0

    incoming = [_fingerprint(m) for m in messages if isinstance(m, dict)]
    known = [_fingerprint(m) for m in remembered]
    if not incoming:
        return messages, 0

    # Where does the client's first turn sit inside what the proxy remembers?
    # 0 means the client re-sent everything; deeper means it dropped the head.
    anchor = 0
    for index, print_ in enumerate(known):
        if print_ == incoming[0]:
            anchor = index
            break
        anchor = -1
    if anchor <= 0:
        return messages, 0

    # Cheap sanity check: the turn after the anchor should also line up.
    if len(incoming) > 1 and len(known) > anchor + 1:
        if known[anchor + 1] != incoming[1]:
            return messages, 0

    restored = [m for m in remembered[:anchor]] + list(messages)
    return restored, anchor


def _trim_to_cap(messages: list, ratio: float = 1.0) -> tuple[list, int]:
    """Keep every system message plus the newest turns that fit the cap.

    Never cuts inside a tool exchange: a `role: tool` message is only valid
    directly after the assistant message that called it, so a cut that would
    strand one walks forward to the next safe boundary instead.
    """
    if not messages:
        return messages, 0

    # ratio scales estimated tokens to the provider's real count, so the budget
    # is expressed in the estimator's own units.
    cap = int(CONTEXT_CAP / max(ratio, 0.2))

    systems = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    rest = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]

    system_tokens = sum(_msg_tokens(m) for m in systems)
    note_tokens = (len(TRIM_NOTE) // 4 + 4) if systems else 0
    budget = max(cap - system_tokens - note_tokens, cap // 4)

    # Walk backwards from the newest message until the budget is spent.
    total = 0
    cut = len(rest)
    for index in range(len(rest) - 1, -1, -1):
        cost = _msg_tokens(rest[index])
        if total + cost > budget and cut < len(rest):
            break
        total += cost
        cut = index
        if total >= budget:
            break

    if cut == 0:
        return messages, 0

    # Do not begin the kept tail with an orphaned tool result or a tool call
    # whose results were kept out.
    while cut < len(rest):
        msg = rest[cut]
        role = msg.get("role") if isinstance(msg, dict) else None
        if role == "tool":
            cut += 1
            continue
        if role == "assistant" and isinstance(msg.get("tool_calls"), list) and msg["tool_calls"]:
            cut += 1
            continue
        break

    if cut >= len(rest):
        # The tail alone overflows the cap: hard-trim the newest message body
        # rather than send something upstream will silently amputate.
        last = dict(rest[-1])
        text = _msg_text(last)
        keep = max(budget * 4, 512)
        if len(text) > keep:
            last["content"] = text[-keep:]
            return systems + [last], 1
        return messages, 0

    dropped = cut
    rebuilt = list(systems)
    if systems:
        rebuilt[-1] = dict(rebuilt[-1])
        rebuilt[-1]["content"] = _msg_text(rebuilt[-1]).rstrip() + "\n\n" + TRIM_NOTE
    return rebuilt + rest[cut:], dropped


def apply_context_guard(body: dict, headers=None) -> tuple[dict, dict]:
    """Restore remembered turns and fit the prompt under the usable ceiling.

    Returns the (possibly rewritten) body and a report suitable for a response
    header. The caller forwards whatever comes back; nothing here touches
    anything but `messages` and `max_tokens`.
    """
    report = {"restored": 0, "trimmed": 0, "tokens": 0, "cap": CONTEXT_CAP,
              "ratio": 1.0, "key": None, "est": 0}
    if not CONTEXT_CAP:
        return body, report

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return body, report

    key = _conv_key(body, headers)
    report["key"] = key
    ratio = _RATIO.get(key, 1.0) if key else 1.0
    report["ratio"] = round(ratio, 3)

    messages, restored = _memory_restore(key, messages)
    report["restored"] = restored

    messages, trimmed = _trim_to_cap(messages, ratio)
    report["trimmed"] = trimmed
    report["tokens"] = sum(_msg_tokens(m) for m in messages if isinstance(m, dict))
    # What was actually forwarded. Comparing the *untrimmed* estimate against
    # the provider's count would divide two different prompts and poison the
    # calibration ratio.
    report["est"] = report["tokens"]

    # Leave headroom for the answer inside the same ceiling, so a large
    # max_tokens cannot push the request back over the real limit.
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        ceiling = max(CONTEXT_CAP - OUTPUT_CAP, CONTEXT_CAP // 2)
        if int(report["tokens"] * ratio) + max_tokens > ceiling:
            body["max_tokens"] = max(min(max_tokens, OUTPUT_CAP), 256)

    if restored or trimmed:
        body["messages"] = messages
    _memory_store(key, messages)
    return body, report


def _model_context_fields(m: dict) -> dict:
    """Context metadata, passed through for clients that read model cards.

    DeepAI reports contextWindow/maxTokens per id; OpenAI has no field for it,
    so emit the names the common clients look for (OpenRouter uses
    context_length, LM Studio uses max_context_length).
    """
    ctx = m.get("contextWindow")
    out = m.get("maxTokens")
    # Advertise what the lane can actually deliver, not what the catalog prints.
    # A client that believes 320000 compacts around 300k and loses its system
    # prompt to the upstream head-cut long before then.
    if isinstance(ctx, int) and ctx > 0 and CONTEXT_CAP:
        ctx = min(ctx, CONTEXT_CAP)
    if isinstance(out, int) and out > 0 and OUTPUT_CAP:
        out = min(out, OUTPUT_CAP)
    have_ctx = isinstance(ctx, int) and ctx > 0
    have_out = isinstance(out, int) and out > 0

    fields = {}
    if have_ctx:
        fields.update({
            "context_length": ctx,        # OpenRouter / LiteLLM
            "context_window": ctx,        # Anthropic-ish
            "max_context_length": ctx,    # LM Studio
            "contextWindow": ctx,         # Cline / Roo / Kilo
            "max_input_tokens": ctx,      # LiteLLM
        })
    if have_out:
        fields.update({
            "max_output_tokens": out,     # Anthropic / OpenRouter
            "max_completion_tokens": out, # OpenAI
            "maxTokens": out,             # Cline / Roo
        })
    # OpenRouter nests the numbers here; several agents read only this block.
    if have_ctx or have_out:
        top = {}
        if have_ctx:
            top["context_length"] = ctx
        if have_out:
            top["max_completion_tokens"] = out
        fields["top_provider"] = top
    if m.get("input"):
        fields["input_modalities"] = m["input"]
        fields["architecture"] = {
            "input_modalities": m["input"],
            "output_modalities": ["text"],
        }
    if m.get("name"):
        fields["name"] = m["name"]
    if "reasoning" in m:
        fields["reasoning"] = bool(m.get("reasoning"))
    if "premier" in m:
        fields["premier"] = bool(m.get("premier"))
    return fields


def openai_models_to_anthropic(models: list) -> dict:
    data = []
    for m in models:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        entry = {
            "type": "model",
            "id": m["id"],
            "display_name": m.get("name", m["id"]),
            "created_at": "2025-01-01T00:00:00Z",
        }
        ctx = m.get("contextWindow")
        out = m.get("maxTokens")
        if isinstance(ctx, int) and ctx > 0:
            entry["context_window"] = ctx
        if isinstance(out, int) and out > 0:
            entry["max_tokens"] = out
        data.append(entry)
    return {"data": data, "has_more": False,
            "first_id": None, "last_id": None}


def openai_models_to_openai(models: list) -> dict:
    data = []
    for m in models:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        entry = {
            "id": m["id"],
            "object": "model",
            "created": 1700000000,
            "owned_by": "deepai",
        }
        entry.update(_model_context_fields(m))
        data.append(entry)
    return {"object": "list", "data": data}


# ---------------------------------------------------------------------------
# Anthropic SSE streaming translator
# ---------------------------------------------------------------------------
def stream_anthropic(resp, model: str):
    """Translate DeepAI (OpenAI) SSE into Anthropic SSE event stream."""
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    # message_start
    yield ("message_start", {
        "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant",
                    "content": [], "model": model, "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    })
    yield ("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    usage = {}
    stop_reason = "end_turn"
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if choices:
            ch = choices[0]
            delta = ch.get("delta") or {}
            if isinstance(delta.get("content"), str) and delta["content"]:
                yield ("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": delta["content"]},
                })
            if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                # emit as thinking delta is complex; skip for now, keep in text
                pass
            fr = ch.get("finish_reason")
            if fr:
                stop_reason = {"stop": "end_turn", "length": "max_tokens"}.get(fr, "end_turn")
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]

    yield ("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield ("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": usage.get("completion_tokens", 0)},
    })
    yield ("message_stop", {"type": "message_stop"})


def aggregate_sse(resp) -> dict:
    """Collapse an SSE chat stream into one chat.completion object.

    The upstream lane answers with text/event-stream even when stream=false was
    requested, so every non-streaming client path runs its body through here.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage = {}
    gen_id = None
    model = None
    created = 0

    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            if payload == b"[DONE]":
                break
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        gen_id = obj.get("id") or gen_id
        model = obj.get("model") or model
        created = obj.get("created") or created
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for ch in (obj.get("choices") or []):
            delta = ch.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            if isinstance(delta.get("reasoning"), str):
                reasoning_parts.append(delta["reasoning"])
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    # reasoning lanes emit only reasoning_content at times; expose it as content
    # so clients that never read reasoning_content still receive text.
    if not content and reasoning:
        content = reasoning

    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]

    return {
        "id": gen_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "logprobs": None,
            "finish_reason": finish_reason or "stop",
        }],
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

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
        return http.client.HTTPSConnection(UPSTREAM, timeout=300)

    def _send_json(self, status: int, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _lane_headers(self, metered: bool = True) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DeepAI-Code",
            "X-DeepAI-Code-Theos": LANE_VERSION,
            "X-DeepAI-Code-Instance": os.urandom(16).hex(),
        }
        key = _request_key()
        if key:
            h["Api-Key"] = key
        if MACHINE:
            h["X-DeepAI-Code-Machine"] = MACHINE
        return h

    def _conv_id(self, model: str) -> str:
        cid = (self.headers.get("X-Conversation-Id")
               or self.headers.get("x-conversation-id"))
        if cid:
            return cid
        ip = self.client_address[0] if self.client_address else "unknown"
        return f"{ip}::{model}"

    def _upstream(self, method: str, path: str, body: bytes | None = None,
                  headers: dict | None = None, stream: bool = False):
        conn = self._connect()
        h = headers or self._lane_headers()
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Host", UPSTREAM)
        conn.endheaders(body)
        resp = conn.getresponse()
        return conn, resp

    def _passthrough(self, method: str, path: str) -> None:
        body = self._body()
        conn = self._connect()
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Host", UPSTREAM)
        conn.endheaders(body)
        resp = conn.getresponse()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP:
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
        conn.close()

    def _catalog(self) -> list:
        """Upstream model cards, cached for ten minutes.

        Every client polls /v1/models on startup and each of those calls needs
        the lane headers, so one copy is shared by the list and the
        single-model route.
        """
        models = _MODEL_CACHE["models"]
        if models and time.time() - _MODEL_CACHE["at"] <= 600:
            return models
        try:
            conn, resp = self._upstream("GET", MODELS_PATH)
            raw = resp.read()
            conn.close()
        except Exception as exc:
            _MODEL_CACHE["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[models] {_MODEL_CACHE['error']}", file=sys.stderr, flush=True)
            return models
        if resp.status != 200:
            _MODEL_CACHE["error"] = (f"upstream {resp.status}: "
                                     f"{raw[:200].decode('utf-8', 'replace')}")
            print(f"[models] {_MODEL_CACHE['error']}", file=sys.stderr, flush=True)
            return models
        if not PASSTHROUGH:
            raw = filter_models(raw)
        try:
            fresh = json.loads(raw).get("models", [])
        except (json.JSONDecodeError, AttributeError):
            return models
        _MODEL_CACHE["models"] = fresh
        _MODEL_CACHE["at"] = time.time()
        try:
            import dashboard
            dashboard.MODEL_INFO = {
                m["id"]: m for m in fresh
                if isinstance(m, dict) and m.get("id")}
        except Exception:
            pass
        return fresh

    def _send_model(self, mid: str) -> None:
        """GET /v1/models/<id> - the same card, for agents that fetch one."""
        card = next((m for m in self._catalog()
                     if isinstance(m, dict) and m.get("id") == mid), None)
        if card is None:
            self._send_json(404, {"error": {
                "message": f"The model '{mid}' does not exist or you do not "
                           "have access to it.",
                "type": "invalid_request_error",
                "param": "model",
                "code": "model_not_found",
            }})
            return
        anthropic = "anthropic-version" in {k.lower() for k in self.headers.keys()}
        if anthropic:
            self._send_json(200, openai_models_to_anthropic([card])["data"][0])
        else:
            self._send_json(200, openai_models_to_openai([card])["data"][0])

    def _send_html(self, status: int, html_text: str,
                   extra: dict | None = None) -> None:
        data = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _header(self, name: str) -> str:
        for k, v in self.headers.items():
            if k.lower() == name:
                return v or ""
        return ""

    def _diag(self) -> dict:
        """Why the upstream call fails, measured from inside the container.

        Only reachable through the console gate; prints timings and the first
        bytes of every reply so a 502/504 can be explained without SSH.
        """
        import http.client
        import socket

        out: dict = {"upstream": UPSTREAM, "machine": bool(MACHINE),
                     "lane_theos": LANE_VERSION,
                     "gateway_token": bool(GATEWAY_TOKEN)}
        plan = self._key_plan() or [("none", None)]
        out["keys"] = [label for label, _ in plan]

        t0 = time.time()
        try:
            infos = socket.getaddrinfo(UPSTREAM, 443, proto=socket.IPPROTO_TCP)
            out["dns"] = {"ok": True,
                          "ips": sorted({i[4][0] for i in infos}),
                          "ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            out["dns"] = {"ok": False,
                          "error": f"{type(exc).__name__}: {exc}",
                          "ms": int((time.time() - t0) * 1000)}
            return out

        def probe(label: str, key: str | None, method: str, path: str,
                  body: bytes | None) -> dict:
            h = self._lane_headers()
            if key:
                h["Api-Key"] = key
            t = time.time()
            try:
                conn = http.client.HTTPSConnection(UPSTREAM, timeout=25)
                conn.putrequest(method, path, skip_host=True,
                                skip_accept_encoding=True)
                for k, v in h.items():
                    conn.putheader(k, v)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.putheader("Host", UPSTREAM)
                conn.endheaders(body)
                resp = conn.getresponse()
                head = resp.read(240)
                conn.close()
                return {"status": resp.status, "key": label,
                        "ms": int((time.time() - t) * 1000),
                        "head": head.decode("utf-8", "replace")[:220]}
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}", "key": label,
                        "ms": int((time.time() - t) * 1000)}

        label, key = plan[0]
        out["catalog"] = probe(label, key, "GET", MODELS_PATH, None)
        body = json.dumps({"model": "glm-5.3-flash", "max_tokens": 8,
                           "stream": False,
                           "messages": [{"role": "user", "content": "hi"}]
                           }).encode()
        out["chat"] = probe(label, key, "POST", "/deepai_code/chat/completions",
                            body)
        return out

    def _strip_url_token(self) -> None:
        """Accept /t/<token>/v1/... exactly like /v1/...

        Some hosts consume the Authorization header before the app ever sees
        it, and several clients cannot add custom headers. A token in the path
        works with every SDK that takes a base URL.
        """
        if not GATEWAY_TOKEN:
            return
        self._url_token_ok = False
        head, _, query = self.path.partition("?")
        segs = head.split("/")
        if len(segs) >= 3 and segs[1] == "t" and segs[2] == GATEWAY_TOKEN:
            rest = "/" + "/".join(segs[3:])
            self.path = rest + (("?" + query) if query else "")
            self._url_token_ok = True

    def _token_ok(self, given: str) -> bool:
        given = (given or "").strip()
        if not given:
            return False
        return given == GATEWAY_TOKEN or given in EXTRA_TOKENS

    def _console_token(self) -> str:
        """Token from the header, the ?console= link, or the console cookie."""
        if not GATEWAY_TOKEN:
            return ""
        if getattr(self, "_url_token_ok", False):
            return GATEWAY_TOKEN
        direct = self._header("x-console-token").strip()
        if direct:
            return direct
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        vals = q.get("console") or []
        if vals:
            return vals[0].strip()
        for part in self._header("cookie").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "msconsole":
                return value.strip()
        return ""
    def _is_proxied(self) -> bool:
        return bool({k.lower() for k in self.headers.keys()} & {
            "cf-connecting-ip", "cf-ray", "cf-visitor",
            "x-forwarded-for", "x-real-ip", "x-forwarded-host",
            "x-ms-client-ip",
        })

    def _console_allowed(self) -> bool:
        """The console hands out raw Api-Keys, so it is never public.

        Loopback is trusted when the request did not come through a proxy.
        Behind one (cloudflared, the ModelScope ingress) loopback proves
        nothing, because the proxy itself dials in from 127.0.0.1, so the
        caller must present DEEPAI_GATEWAY_TOKEN instead.
        """
        proxied = self._is_proxied()
        if proxied:
            return self._token_ok(self._console_token())
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _gate_ok(self, path: str) -> bool:
        """Bearer / x-api-key / api-key / ?api_key= must match GATEWAY_TOKEN."""
        if not GATEWAY_TOKEN:
            return True
        if getattr(self, "_url_token_ok", False):
            return True
        if not (path.startswith("/v1") or path.startswith("/models")
                or path.startswith("/deepai_code")):
            return True
        auth = self._header("authorization").strip()
        given = ""
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given:
            for name in ("x-api-key", "api-key", "x-gateway-token",
                         "x-deepai-key"):
                given = self._header(name).strip()
                if given:
                    break
        if not given:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            for name in ("api_key", "token", "key"):
                vals = q.get(name) or []
                if vals:
                    given = vals[0].strip()
                    break
        # a ModelScope Studio consumes Authorization for its own ingress token,
        # so x-api-key still carries the gateway token in that case
        return self._token_ok(given)

    def _route_console(self, method: str, path: str, payload: dict) -> bool:
        """Serve the dashboard and its API. Returns True when handled.

        Loopback callers only, or a token holder when the gateway is public:
        the console hands out raw Api-Keys.
        """
        console_path = (method == "GET" and path in ("/", "/dashboard", "/console")) \
            or path.startswith("/api/")
        if not console_path:
            return False
        if not self._console_allowed():
            # A console page shows an unlock form, so the ModelScope App tab is
            # useful without exposing keys. API paths stay a flat 404.
            if method == "GET" and path in ("/", "/dashboard", "/console"):
                self._send_html(200, CONSOLE_GATE_HTML)
                return True
            # do not leak the console's existence on /api/*, and do not hand the
            # path to the upstream either: it answers 200 on "/" and 401 on
            # "/api/...", which is just confusing in a client
            self._send_json(404, {"error": "not found"})
            return True
        if method == "GET" and path in ("/", "/dashboard", "/console"):
            try:
                import dashboard
            except Exception as exc:
                self._send_json(500, {"error": f"dashboard unavailable: {exc}"})
                return True
            # a ?console=<token> link pins a cookie so the page's own /api
            # calls keep working without the query string on every fetch
            extra = {}
            if GATEWAY_TOKEN and self._header("cookie").find("msconsole=") < 0:
                if self._token_ok(self._console_token()):
                    # the ModelScope App tab is an iframe on another site, so a
                    # Lax cookie would not ride along on its /api calls
                    same = "None; Secure" if self._is_proxied() else "Lax"
                    extra["Set-Cookie"] = (
                        f"msconsole={self._console_token()}; Path=/; "
                        f"Max-Age=604800; SameSite={same}")
            self._send_html(200, dashboard.PAGE, extra)
            return True
        try:
            import dashboard
        except Exception as exc:
            self._send_json(500, {"error": f"dashboard unavailable: {exc}"})
            return True
        try:
            port = self.server.server_address[1]
            dashboard.PROXY_BASE = f"http://127.0.0.1:{port}"
            host = (self._header("x-forwarded-host")
                    or self._header("host")).strip()
            proto = self._header("x-forwarded-proto").strip() or "https"
            if host and host.split(":")[0] not in ("127.0.0.1", "localhost"):
                base = f"{proto}://{host.rstrip('/')}"
                dashboard.REQUEST_BASE = (f"{base}/t/{GATEWAY_TOKEN}"
                                          if GATEWAY_TOKEN else base)
            status, obj = dashboard.handle_api(method, path, payload)
        except Exception as exc:
            status, obj = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self._send_json(status, obj)
        return True

    def _key_plan(self) -> list[tuple[str, str | None]]:
        """[(label, key)] best first, so a capped account fails over on its own."""
        try:
            import dashboard
            plan = dashboard.key_candidates()
            if plan:
                return plan
        except Exception:
            pass
        return [("", API_KEY)] if API_KEY else [("", None)]

    def _note_failure(self, label: str, status: int, raw: bytes) -> None:
        try:
            import dashboard
            dashboard.note_failure(label, status, raw.decode("utf-8", "replace"))
        except Exception:
            pass

    def _note_success(self, label: str, usage: dict | None = None) -> None:
        try:
            import dashboard
            dashboard.note_success(label, usage)
        except Exception:
            pass

    def _chat_upstream(self, payload: bytes, stream: bool):
        """POST the chat lane, walking the keyring when a key is refused.

        Returns (conn, resp, label, (status, raw)) - the last two are None/None
        on success and label is carried onto the reply for the console.
        """
        last: tuple[int, bytes] = (0, b"")
        for label, key in self._key_plan():
            headers = self._lane_headers()
            if key:
                headers["Api-Key"] = key
            try:
                conn, resp = self._upstream("POST", "/deepai_code/chat/completions",
                                            body=payload, headers=headers,
                                            stream=stream)
            except Exception as exc:
                last = (502, json.dumps({"error": {
                    "message": f"upstream {type(exc).__name__}: {exc}",
                    "type": "upstream_error"}}).encode())
                self._note_failure(label, 502, last[1])
                continue
            if resp.status == 200:
                self._note_success(label)
                return conn, resp, label, None
            raw = resp.read()
            conn.close()
            last = (resp.status, raw)
            self._note_failure(label, resp.status, raw)
        return None, None, None, last

    # ------------------------------------------------------------------ #
    def do_GET(self):
        self._strip_url_token()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = parsed.query

        if self._route_console("GET", path, {}):
            return

        if path == "/diag":
            if not self._console_allowed():
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, self._diag())
            return

        if not self._gate_ok(path):
            self._send_json(401, {"error": {
                "message": "Invalid API key for this gateway.",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }})
            return

        if path in ("/v1/models", "/models"):
            models = self._catalog()
            if not models:
                self._send_json(502, {
                    "error": "upstream model catalog unavailable",
                    "detail": _MODEL_CACHE.get("error")})
                return
            # Anthropic-style wants /v1/models to return {data, has_more, ...}
            # OpenAI wants {object, data}. Serve OpenAI shape by default; if the
            # client sent anthropic-version header, serve Anthropic shape.
            anthropic = "anthropic-version" in {k.lower() for k in self.headers.keys()}
            if anthropic:
                self._send_json(200, openai_models_to_anthropic(models))
            else:
                self._send_json(200, openai_models_to_openai(models))
            return

        if path.startswith(("/v1/models/", "/models/")):
            self._send_model(urllib.parse.unquote(
                path.split("/models/", 1)[1]).strip("/"))
            return

        # DeepAI native paths pass through
        self._passthrough("GET", self.path)

    def do_POST(self):
        self._strip_url_token()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        body = self._body() or b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if self._route_console("POST", path, data if isinstance(data, dict) else {}):
            return

        if not self._gate_ok(path):
            self._send_json(401, {"error": {
                "message": "Invalid API key for this gateway.",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }})
            return

        anthropic = "anthropic-version" in {k.lower() for k in self.headers.keys()}

        # --- OpenAI / Anthropic chat -> DeepAI chat/completions --- #
        if path in ("/v1/chat/completions", "/v1/messages",
                    "/deepai_code/chat/completions"):
            stream = bool(data.get("stream", False))

            if path == "/v1/messages":
                oai_body = anthropic_to_openai(data)
            else:
                oai_body = dict(data)

            if not PASSTHROUGH:
                if not model_allowed(oai_body.get("model")):
                    # fall back to the first detected free model, not a 400
                    oai_body["model"] = first_allowed_model()
            stream = bool(oai_body.get("stream", False))

            oai_body, guard = apply_context_guard(oai_body, self.headers)

            payload = json.dumps(oai_body).encode()
            conn, resp, served_by, failure = self._chat_upstream(payload, stream)
            if conn is None:
                status, raw = failure or (502, b'{"error":"no key available"}')
                self._send_json(status, {"error": raw.decode("utf-8", "replace")[:500],
                                         "tried": [l for l, _ in self._key_plan()]})
                return

            is_anthropic = (path == "/v1/messages") or anthropic
            guard_header = "restored=%d trimmed=%d tokens=%d ratio=%.2f cap=%d" % (
                guard["restored"], guard["trimmed"], guard["tokens"],
                guard["ratio"], guard["cap"])

            if stream:
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/event-stream" if not is_anthropic
                                 else "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-DeepAI-Key", "public")
                self.send_header("X-Context-Guard", guard_header)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                model = oai_body.get("model", first_allowed_model())

                if is_anthropic:
                    for event, payload in stream_anthropic(resp, model):
                        self.wfile.write(f"event: {event}\n".encode())
                        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                        self.wfile.flush()
                else:
                    # OpenAI SSE: pass through, but rewrite model name in chunks
                    # and capture usage for the per-account token graph.
                    captured_usage: dict = {}
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        for line in chunk.decode("utf-8", "replace").splitlines():
                            if line.startswith("data:") and line.strip() != "data: [DONE]":
                                try:
                                    obj = json.loads(line[5:].strip())
                                    if isinstance(obj.get("usage"), dict):
                                        captured_usage = obj["usage"]
                                except json.JSONDecodeError:
                                    pass
                    if captured_usage:
                        self._note_success(served_by, captured_usage)
                        note_prompt_usage(guard.get("key"), guard.get("est", 0),
                                          captured_usage.get("prompt_tokens"),
                                          guard.get("trimmed", 0))
                conn.close()
                return

            # non-streaming
            raw = resp.read()
            conn.close()
            try:
                oai_resp = json.loads(raw)
            except json.JSONDecodeError:
                # upstream always answers SSE; collapse it
                import io
                oai_resp = aggregate_sse(io.BytesIO(raw))

            if is_anthropic:
                self._send_json(200, openai_to_anthropic(
                    oai_resp, oai_body.get("model", first_allowed_model())))
                note_prompt_usage(guard.get("key"), guard.get("est", 0),
                                  (oai_resp.get("usage") or {}).get("prompt_tokens"),
                                  guard.get("trimmed", 0))
            else:
                # no Content-Length here used to mean "body ends when the socket
                # closes". http.server keeps it alive, so any buffering proxy in
                # front (nginx, the ModelScope ingress) waited until it timed out.
                body = json.dumps(oai_resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-DeepAI-Key", "public")
                self.send_header("X-Context-Guard", guard_header)
                self.end_headers()
                self.wfile.write(body)
                self._note_success(served_by, oai_resp.get("usage"))
                note_prompt_usage(guard.get("key"), guard.get("est", 0),
                                  (oai_resp.get("usage") or {}).get("prompt_tokens"),
                                  guard.get("trimmed", 0))
            return

        # --- Anthropic count_tokens (estimate) --- #
        if path == "/v1/messages/count_tokens":
            # Same pessimistic estimator the guard uses, so a client's own
            # compaction math matches what the guard will actually do.
            msgs = data.get("messages") or []
            total = 0
            for m in msgs:
                if isinstance(m, dict):
                    total += _msg_tokens(m)
            sys_ = data.get("system", "")
            if isinstance(sys_, list):
                sys_ = " ".join(b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in sys_)
            if sys_:
                total += len(str(sys_)) // 3 + 16
            self._send_json(200, {"input_tokens": max(1, total)})
            return

        # --- DeepAI native / account paths pass through --- #
        self._passthrough("POST", self.path)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main(argv: list[str]) -> int:
    global PASSTHROUGH, EXPLICIT_MODELS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", action="append", metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.model:
        EXPLICIT_MODELS = set(args.model)
    PASSTHROUGH = args.all

    server = Server((args.host, args.port), Proxy)
    server.verbose = args.verbose

    if args.all:
        keep = "everything (passthrough, includes premier)"
    elif EXPLICIT_MODELS is not None:
        keep = f"{len(EXPLICIT_MODELS)} pinned ids"
    else:
        keep = "auto-detected 0-Theos models"
    print(f"deepai openai+anthropic gateway  http://{args.host}:{args.port}")
    print(f"  upstream  https://{UPSTREAM}")
    print(f"  keeping   {keep}")
    if not args.all:
        for mid in free_ids():
            print(f"            {mid}")
    print()
    print("  OpenAI base URL:     http://%s:%d/v1" % (args.host, args.port))
    print("  Anthropic base URL:  http://%s:%d" % (args.host, args.port))
    print("  DeepAI native:       http://%s:%d/deepai_code/..." % (args.host, args.port))
    print()
    print("  ctrl-c to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
