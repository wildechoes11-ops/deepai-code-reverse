"""Which model produced each recorded turn, and how big those prompts got.

Joins provider turns to runs so the big prompts can be attributed to a model
instead of guessed at. Counts and token numbers only -- no message content.
"""
import json
import os
import sqlite3

HOME = os.environ.get("DEEPAI_CODE_HOME") or os.path.join(
    os.path.expanduser("~"), ".deepai-code")
DB = os.path.join(HOME, "userdata", "statev2.sqlite")

con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
cur = con.cursor()
cur.execute("select name from sqlite_master where type='table'")
tables = [r[0] for r in cur.fetchall()]
print("tables:", [t for t in tables if "run" in t or "thread" in t])

for table in tables:
    if "run" not in table:
        continue
    cur.execute(f"pragma table_info({table})")
    cols = [c[1] for c in cur.fetchall()]
    if not cols:
        continue
    print(f"\n{table}: {cols}")
    cur.execute(f"select * from {table} limit 1")
    row = cur.fetchone()
    if row:
        print("  sample:", {k: str(v)[:80] for k, v in zip(cols, row)})
con.close()
