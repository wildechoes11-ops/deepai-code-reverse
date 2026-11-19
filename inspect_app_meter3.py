"""Dump the largest recorded turns with their full token usage.

Needed to explain one number: a 289,080-token turn on a lane that plateaus
near 49,000. Either usage is computed from the whole request body, or cache
tokens are folded in. Counts only.
"""
import json
import os
import sqlite3

HOME = os.environ.get("DEEPAI_CODE_HOME") or os.path.join(
    os.path.expanduser("~"), ".deepai-code")
DB = os.path.join(HOME, "userdata", "statev2.sqlite")

con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
cur = con.cursor()
cur.execute("select thread_id, ordinal, payload_json from "
            "orchestration_v2_projection_provider_turns")

rows = []
for tid, ordinal, raw in cur.fetchall():
    try:
        doc = json.loads(raw or "{}")
    except Exception:
        continue
    usage = doc.get("tokenUsage") or {}
    if isinstance(usage.get("usedTokens"), int):
        rows.append((usage["usedTokens"], tid, ordinal, usage,
                     doc.get("turnTokenUsage")))

rows.sort(reverse=True)
for used, tid, ordinal, usage, turn in rows[:4]:
    print(f"used={used} thread={tid[:12]} ord={ordinal}")
    print("  tokenUsage:", json.dumps(usage))
    print("  turnTokenUsage:", json.dumps(turn)[:260])
con.close()
