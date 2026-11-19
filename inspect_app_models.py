"""Attribute every recorded provider turn to its model, then compare prompt sizes.

The global histogram mixed a 1,048,576 fallback catalog with a 320,000 one, so
"max usedTokens 289,080" could be a premier model with a real large window or a
free flash model losing its head. This joins runs to turns to tell them apart.
Counts and token numbers only.
"""
import json
import os
import sqlite3
import urllib.parse

HOME = os.environ.get("DEEPAI_CODE_HOME") or os.path.join(
    os.path.expanduser("~"), ".deepai-code")
DB = os.path.join(HOME, "userdata", "statev2.sqlite")

con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
cur = con.cursor()

runs = {}
cur.execute("select run_id, payload_json from orchestration_v2_projection_runs")
for run_id, raw in cur.fetchall():
    try:
        d = json.loads(raw or "{}")
    except Exception:
        continue
    model = d.get("modelSelection") or d.get("model") or {}
    if isinstance(model, dict):
        model = model.get("model") or model.get("id") or "?"
    runs[run_id] = str(model)

cur.execute("select run_attempt_id, payload_json from "
            "orchestration_v2_projection_provider_turns")
by_model = {}
for attempt_id, raw in cur.fetchall():
    try:
        d = json.loads(raw or "{}")
    except Exception:
        continue
    usage = d.get("tokenUsage") or {}
    used = usage.get("usedTokens")
    if not isinstance(used, int):
        continue
    run_id = attempt_id or ""
    if ":attempt:" in run_id:
        run_id = run_id.split(":attempt:")[0]
    if run_id.startswith("run-attempt:run:"):
        decoded = urllib.parse.unquote(run_id[len("run-attempt:run:"):])
        run_id = decoded if decoded.startswith("run:") else "run:" + decoded
    by_model.setdefault(runs.get(run_id, "?"), []).append(used)

print(f"{'model':<24}{'turns':>6}{'p50':>10}{'max':>10}{'>43516':>9}")
for model, vals in sorted(by_model.items(), key=lambda kv: -max(kv[1])):
    vals.sort()
    over = sum(1 for v in vals if v > 43516)
    print(f"{model:<24}{len(vals):>6}{vals[len(vals)//2]:>10}{vals[-1]:>10}{over:>9}")
con.close()
