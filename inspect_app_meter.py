"""Read-only inspection of the desktop app's context meter history.

Prints the distinct `tokenUsage.maxTokens` values the app has recorded per
provider turn, plus how close any turn ever came to the compaction threshold.
No message content, no keys -- counts only.
"""
import json
import os
import sqlite3

# App data sits under the current user's profile. Point DEEPAI_CODE_HOME at a
# different install if the app lives somewhere else.
HOME = os.environ.get("DEEPAI_CODE_HOME") or os.path.join(
    os.path.expanduser("~"), ".deepai-code")
DB = os.path.join(HOME, "userdata", "statev2.sqlite")

con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
cur = con.cursor()

cur.execute("select name from sqlite_master where type='table'")
tables = [r[0] for r in cur.fetchall()]
target = [t for t in tables if "provider_turn" in t]
print("provider turn tables:", target)

for table in target:
    cur.execute(f"select payload_json from {table}")
    rows = cur.fetchall()
    hist = {}
    used = []
    for (raw,) in rows:
        try:
            d = json.loads(raw or "{}")
        except Exception:
            continue
        usage = d.get("tokenUsage")
        if not isinstance(usage, dict):
            usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        hist[usage.get("maxTokens")] = hist.get(usage.get("maxTokens"), 0) + 1
        if isinstance(usage.get("usedTokens"), int):
            used.append(usage["usedTokens"])
    print(f"\n{table}: {len(rows)} rows")
    print("  maxTokens histogram:", hist)
    if used:
        used.sort()
        print(f"  usedTokens: n={len(used)} max={used[-1]} "
              f"p50={used[len(used)//2]} p95={used[int(len(used)*0.95)]}")
        for cap in (320000, 256000, 40000, 55000):
            threshold = cap - 16384
            over = sum(1 for u in used if u > threshold)
            print(f"  if contextWindow={cap}: compaction fires at {threshold}, "
                  f"turns over it = {over}")
con.close()
