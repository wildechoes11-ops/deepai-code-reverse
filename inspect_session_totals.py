"""Totals across every stored session: how much conversation is on disk.

Shows the whole persisted history versus the amount that can reach the model.
Counts only.
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
total_bytes = 0
messages = 0
edits = 0
chars = 0
for path in files:
    total_bytes += os.path.getsize(path)
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "context_edit":
            edits += 1
        elif rec.get("type") == "message":
            messages += 1
            content = (rec.get("message") or {}).get("content")
            chars += len(content) if isinstance(content, str) else len(json.dumps(content))

print(f"sessions      : {len(files)}")
print(f"on disk       : {total_bytes/1024/1024:.2f} MB")
print(f"messages      : {messages:,}")
print(f"context_edits : {edits}")
print(f"content chars : {chars:,}  (~{chars//4:,} tokens persisted)")
