"""What the app actually stores, and what it replays.

The claim to verify: the desktop app never forgets. This reads the biggest
session file and reports, per role, how many messages and how many characters
are persisted. If nothing was dropped, the totals equal the whole conversation
even though the provider only ever ingested ~49k tokens of it.
Character counts and counts only -- no message text.
"""
import glob
import json
import os

# Per-user app data; override with DEEPAI_CODE_HOME for a different install.
HOME = os.environ.get("DEEPAI_CODE_HOME") or os.path.join(
    os.path.expanduser("~"), ".deepai-code")
ROOT = os.path.join(HOME, "userdata", "deepai", "pi", "deepai", "sessions")

files = sorted(glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True),
               key=os.path.getsize, reverse=True)

for path in files[:3]:
    roles = {}
    total_chars = 0
    tool_block_chars = 0
    messages = 0
    edits = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "context_edit":
            edits += 1
            continue
        if rec.get("type") != "message":
            continue
        msg = rec.get("message") or {}
        role = msg.get("role", "?")
        content = msg.get("content")
        size = len(content) if isinstance(content, str) else len(json.dumps(content))
        roles.setdefault(role, [0, 0])
        roles[role][0] += 1
        roles[role][1] += size
        total_chars += size
        messages += 1
        for block in (content if isinstance(content, list) else []):
            if isinstance(block, dict) and block.get("type") in ("tool_result", "tool_use"):
                tool_block_chars += len(json.dumps(block))

    print(f"\n{os.path.basename(path)}  {os.path.getsize(path)/1024:.0f} KB")
    print(f"  messages={messages} context_edits={edits}")
    print(f"  total content chars={total_chars:,}  ~tokens={total_chars//4:,}")
    print(f"  chars inside tool blocks={tool_block_chars:,}")
    for role, (n, chars) in sorted(roles.items(), key=lambda kv: -kv[1][1]):
        print(f"    {role:<10} n={n:<5} chars={chars:>10,} ~tok={chars//4:>8,}")
