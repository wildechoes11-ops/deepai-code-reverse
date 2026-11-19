"""Show the shape of the desktop app's session store.

Prints one example of each record type found in a session .jsonl, with long
strings truncated, so the storage format is visible: how a message is stored,
and what `context_edit` actually does (that is the compaction record).
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
path = files[0]
print("file:", os.path.basename(path), f"{os.path.getsize(path)/1024:.0f} KB")

SEEN = {}
counts = {}
for line in open(path, encoding="utf-8", errors="replace"):
    try:
        rec = json.loads(line)
    except Exception:
        continue
    kind = rec.get("type")
    counts[kind] = counts.get(kind, 0) + 1
    if kind not in SEEN:
        SEEN[kind] = rec

print("counts:", counts)


def trim(obj, depth=0):
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: trim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [trim(v, depth + 1) for v in obj[:3]] + (["+%d more" % (len(obj) - 3)] if len(obj) > 3 else [])
    if isinstance(obj, str) and len(obj) > 220:
        return obj[:220] + f"...[{len(obj)} chars]"
    return obj


for kind, rec in SEEN.items():
    print("\n=== %s ===" % kind)
    print(json.dumps(trim(rec), indent=1)[:1400])
