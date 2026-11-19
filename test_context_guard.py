"""Smoke test for the proxy context guard.

Imports deepai-proxy.py by path (the filename has a dash) and exercises the
four behaviours that matter: skip-when-small, head-preserving trim, remembered
first turn, and tool-call safety across a cut.
"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("deepai_proxy", "deepai-proxy.py")
proxy = importlib.util.module_from_spec(spec)
sys.modules["deepai_proxy"] = proxy
spec.loader.exec_module(proxy)

FILLER = "the quick brown fox jumps over the lazy dog near the river bank "


def short_conv():
    return [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "fix the build"},
    ]


def long_conv(pairs):
    msgs = [{"role": "system", "content": "You are a coding agent. " + "Rules. " * 50}]
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"turn {i}: {FILLER}"})
        msgs.append({"role": "assistant", "content": f"done {i}: {FILLER}"})
    msgs.append({"role": "user", "content": "FINALQUESTION"})
    return msgs


fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


# 1. small conversation is untouched
body = {"model": "glm-5.3-flash", "messages": short_conv(), "max_tokens": 100}
out, rep = proxy.apply_context_guard(body, {})
check("small prompt untouched", rep["trimmed"] == 0 and rep["restored"] == 0,
      f"trimmed={rep['trimmed']} restored={rep['restored']}")
check("small prompt same length", len(out["messages"]) == 4)

# 2. oversized conversation keeps the system message and the newest turn
big = long_conv(2000)
body = {"model": "glm-5.3-flash", "messages": big, "max_tokens": 4096}
out, rep = proxy.apply_context_guard(body, {})
msgs = out["messages"]
check("long prompt trimmed", rep["trimmed"] > 0, f"trimmed={rep['trimmed']} tokens={rep['tokens']}")
check("system message survives", msgs[0]["role"] == "system"
      and "You are a coding agent" in msgs[0]["content"])
check("trim note present", proxy.TRIM_NOTE in msgs[0]["content"])
check("newest turn survives", msgs[-1]["content"] == "FINALQUESTION")
check("under cap", rep["tokens"] <= proxy.CONTEXT_CAP, f"tokens={rep['tokens']}")

# 3. a tail-only follow-up gets its head back from memory
first = [
    {"role": "system", "content": "You are a coding agent."},
    {"role": "user", "content": "my project is a rust webserver"},
    {"role": "assistant", "content": "understood"},
    {"role": "user", "content": "add a health endpoint"},
    {"role": "assistant", "content": "added"},
]
headers = {"X-Conversation-Id": "conv-test-1"}
body = {"model": "glm-5.3-flash", "messages": first}
proxy.apply_context_guard(body, headers)

tail = [
    {"role": "user", "content": "add a health endpoint"},
    {"role": "assistant", "content": "added"},
    {"role": "user", "content": "now add metrics"},
]
body = {"model": "glm-5.3-flash", "messages": tail}
out, rep = proxy.apply_context_guard(body, headers)
check("tail restored from memory", rep["restored"] == 3, f"restored={rep['restored']}")
check("restored system first", out["messages"][0]["role"] == "system")
check("restored original ask", "rust webserver" in proxy._msg_text(out["messages"][1]))
check("new question last", out["messages"][-1]["content"] == "now add metrics")

# 4. a cut never strands a tool result
tool_conv = [{"role": "system", "content": "agent"}]
for i in range(1200):
    tool_conv.append({"role": "user", "content": f"step {i} {FILLER}"})
    tool_conv.append({"role": "assistant", "content": "",
                      "tool_calls": [{"id": f"c{i}", "type": "function",
                                      "function": {"name": "read", "arguments": "{}"}}]})
    tool_conv.append({"role": "tool", "tool_call_id": f"c{i}", "content": FILLER * 4})
tool_conv.append({"role": "user", "content": "LAST"})
body = {"model": "glm-5.3-flash", "messages": tool_conv}
out, rep = proxy.apply_context_guard(body, {"X-Conversation-Id": "conv-test-2"})
kept = out["messages"]
check("tool conversation trimmed", rep["trimmed"] > 0, f"trimmed={rep['trimmed']}")
check("tail does not start with orphan tool", kept[1]["role"] != "tool",
      f"first non-system role={kept[1]['role']}")
check("tail does not start with dangling tool_call",
      not (kept[1]["role"] == "assistant" and kept[1].get("tool_calls")))
check("last turn intact", kept[-1]["content"] == "LAST")

# 5. max_tokens is clamped when the prompt is already large
body = {"model": "glm-5.3-flash", "messages": long_conv(2000), "max_tokens": 131072}
out, rep = proxy.apply_context_guard(body, {})
check("max_tokens clamped", out["max_tokens"] <= proxy.OUTPUT_CAP,
      f"max_tokens={out['max_tokens']}")

# 6. the catalog advertises the honest number, not 320000
fields = proxy._model_context_fields({"contextWindow": 320000, "maxTokens": 131072})
check("context_length clamped", fields["context_length"] == proxy.CONTEXT_CAP,
      f"context_length={fields['context_length']}")
check("max_completion_tokens clamped", fields["max_completion_tokens"] == proxy.OUTPUT_CAP)

print()
if fails:
    print("FAILURES:", ", ".join(fails))
    sys.exit(1)
print("all guard checks passed")
