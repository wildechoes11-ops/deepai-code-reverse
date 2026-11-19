"""Is the app's context meter client-side or provider-reported?

If `usedTokens` climbs monotonically with the transcript while `inputTokens`
from the provider plateaus (because the head is being cut), the meter is a
local estimate of what was SENT -- and compaction would work correctly once
contextWindow is honest. If both plateau together, the meter is reading the
amputated count and compaction can never fire.
Counts only, no content.
"""
import json
import os
import sqlite3
from collections import Counter

HOME = os.environ.get("DEEPAI_CODE_HOME") or os.path.join(
    os.path.expanduser("~"), ".deepai-code")
DB = os.path.join(HOME, "userdata", "statev2.sqlite")

con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
cur = con.cursor()
cur.execute("select thread_id, ordinal, payload_json from "
            "orchestration_v2_projection_provider_turns")
rows = cur.fetchall()

counts = Counter(r[0] for r in rows)
thread_id, _ = counts.most_common(1)[0]
print(f"thread {thread_id[:12]} turns={counts.most_common(1)[0][1]}")
print(f"{'ord':>4}{'used':>10}{'input':>10}{'cached':>10}{'output':>9}")

for tid, ordinal, raw in rows:
    if tid != thread_id:
        continue
    try:
        usage = (json.loads(raw or "{}").get("tokenUsage") or {})
    except Exception:
        continue
    print(f"{ordinal:>4}{usage.get('usedTokens'):>10}"
          f"{usage.get('inputTokens'):>10}"
          f"{usage.get('cachedInputTokens'):>10}"
          f"{usage.get('outputTokens'):>9}")

con.close()
