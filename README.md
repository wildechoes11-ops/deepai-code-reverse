# deepai-code-reverse

A local, OpenAI-compatible proxy for the DeepAI Code lane, plus the probes and
inspectors that produced it.

The proxy exists because of one measured fact: **the DeepAI Code model catalog
advertises `contextWindow: 320000` for every model, and the serving tier delivers
roughly 43,500 prompt tokens before it silently discards the front of the
prompt** — system message included — returning HTTP 200 with `finish_reason:
stop` as if nothing happened.

The desktop app never notices, because the app stores the entire transcript on
disk and replays the whole branch every turn. Its memory is a file, not a
window. So the app appears to remember 320k while the model is receiving the
tail of a 43k prompt.

This proxy sits between any client and the lane and makes the numbers honest:

- advertises a **real** context cap (`40000`) and output cap (`8192`) instead of
  the fictional 320000, so agents size their prompts correctly;
- **restores** conversation history server-side (LRU keyed by conversation id),
  so a client that sends only a tail still gets its full branch replayed;
- **trims to the cap itself**, head-preserving, tool-pair safe — instead of
  letting upstream decapitate the prompt;
- **self-calibrates** its token estimator against the provider's reported
  `prompt_tokens` and reports what it did in an `X-Context-Guard` header.

Stdlib only. No pip install, no dependencies.

---

## Quick start

```powershell
# 0. get a key (sign in, or read the one the desktop app already stored)
python auth.py                      # full sign-in flow, saves + verifies
python auth.py --link               # just the sign-in URL, no listener
python auth.py --from-app           # extract the key off a signed-in machine
python auth.py list                 # show the keyring

# 1. point the proxy at that keyring and boot it
python deepai-proxy.py --port 8788

# 2. verify the guard is live
curl.exe -s -D - -o NUL http://127.0.0.1:8788/v1/models
```

The banner line confirms the effective caps:

```
ctx=40000 out=8192
```

Then point any OpenAI-compatible client at it:

```
base_url = http://127.0.0.1:8788/v1
api_key  = anything (or DEEPAI_GATEWAY_TOKEN if you set one)
model    = glm-5.3-flash, deepseek-v4.1-flash, minimax-m3, ...
```

To run the DeepAI Code desktop app itself against the local filter instead, use
`launch-deepai-filtered.ps1`.

---

## Getting a key

`auth.py` drives the same four-step sign-in the official client uses, from a
terminal. Nothing tricked, nothing injected — the browser is your real browser,
so a captcha or a verification step is a click, not a failure mode.

```
1. GET   https://api.deepai.org/deepai_code/desktop_auth/?state=<hex>&port=<P>
2. the browser lands on deepai.org, and you sign up or log in there
3. it 302s back to   http://127.0.0.1:<P>/auth-callback?code=<code>&state=<hex>
4. POST  https://api.deepai.org/deepai_code/desktop_auth/exchange/ {"code": ...}
   ->    {"api_key": "...", "username": "...", "is_pro": bool}
```

Step 4 is the mint. The web signup never shows a key; the exchange response
carries it.

| mode | what it does |
|---|---|
| `python auth.py` | full flow: free port, state, browser, loopback catch, exchange, save, verify |
| `python auth.py --link` | print the sign-in URL and exit — no listener, no browser |
| `python auth.py --exchange CODE` | exchange a `?code=` or a whole callback URL you already have |
| `python auth.py --from-app` | extract the key the desktop app already stored — no network |
| `python auth.py list` | show the keyring, keys masked |
| `python auth.py verify` | hit `/theos` with every stored key |

The key is written to `deepai-keyring.json` in the format the proxy reads:

```json
{"keys": [{"label": "default", "api_key": "...", "username": "...",
           "is_pro": false, "added": "2026-11-19T12:00:00Z"}]}
```

Saving an existing label replaces that entry, so re-running the flow refreshes a
key instead of stacking duplicates. File permissions are tightened as far as the
platform allows.

`--from-app` is the offline route. The desktop app stores its key as plain JSON —
no OS keyring, no encryption:

```
%USERPROFILE%\.deepai-code\userdata\secrets\deepai-account.bin
{"apiKey": "...", "username": "...", "isPro": false}
```

Reading it needs no admin rights and no network. Candidate homes are
`DEEPAI_CODE_HOME`, `%USERPROFILE%\.deepai-code`, then
`%APPDATA%\deepai-code-v2`.

Accepted options: `--out` (keyring path), `--label` (entry name), `--port`
(preferred callback port), `--timeout` (default 300s), `--no-browser`.

**The keyring is in `.gitignore`.** Never commit it.

---

## Configuration

Every knob is an environment variable. Nothing is baked in.

| variable | default | meaning |
|---|---|---|
| `DEEPAI_API_KEY` | — | upstream credential, if you have one directly |
| `DEEPAI_KEYRING` | `deepai-keyring.json` | path to a keyring file to read the key from |
| `DEEPAI_KEY_LABEL` | — | which keyring entry to select |
| `DEEPAI_GATEWAY_TOKEN` | *(unset)* | bearer clients must present to *this* proxy; unset means accept any |
| `DEEPAI_EXTRA_TOKENS` | — | comma-separated additional accepted bearers |
| `DEEPAI_MACHINE` | derived | machine id override (the lane fingerprints the host) |
| `DEEPAI_CODE_HOME` | `%USERPROFILE%\.deepai-code` | where the desktop app keeps its data, used by the inspectors |
| `DEEPAI_CODE_EXE` | `%LOCALAPPDATA%\Programs\deepai-code-desktop\DeepAI Code.exe` | desktop app path for `launch-deepai-filtered.ps1` |
| `DEEPAI_CONTEXT_CAP` | `40000` | advertised + enforced prompt ceiling; `0` disables the guard |
| `DEEPAI_OUTPUT_CAP` | `8192` | advertised + enforced completion ceiling |
| `DEEPAI_MEMORY_TURNS` | `600` | messages retained per conversation in the LRU |
| `DEEPAI_MEMORY_SESSIONS` | `64` | conversations retained |

```powershell
$env:DEEPAI_KEYRING     = "C:\path\to\deepai-keyring.json"
$env:DEEPAI_CONTEXT_CAP = "40000"
$env:DEEPAI_OUTPUT_CAP  = "8192"
python deepai-proxy.py --port 8788 --verbose
```

CLI flags: `--port` (default `8788`), `--host` (`127.0.0.1`), `--model ID`
(repeatable, restrict the catalogue), `--all` (expose every model, not just the
0-Theos set), `--verbose`.

**Keys are never committed and never logged.** The proxy reports counts, roles,
lengths and ids only.

---

## Why the cap is 40000 and not 320000

Measured with the probes in this repo against this lane, across free and premier
models:

| sent (est. tokens) | `prompt_tokens` returned | head | mid | tail |
|---|---|---|---|---|
| 33,595 | 42,132 | OK | OK | OK |
| — | 43,516 | OK | OK | OK |
| 67,440 | 46,739 | LOST | LOST | LOST |
| — | 43,813 | LOST | LOST | LOST |
| — | 49,511 | LOST | LOST | LOST |

The knee sits between **43,516 (delivered whole)** and **43,813 (lossy)**; the
plateau drifts between ~43.5k and ~49.5k depending on the backend that answers.
A premier model (`minimax-m3`, 2 Theos) shows the same wall, so it is not a
free-tier limit.

Truncation is **tail-preserving**. A canary probe confirms the system message is
dropped along with the head while mid and tail survive (`SYSTEM=LOST` at
`prompt_tokens = 47,205`).

`40000` is therefore a deliberate margin below the worst observed knee: enforce
a ceiling you can actually reach, rather than advertise a number the lane
ignores.

---

## Layout

| file | purpose |
|---|---|
| `auth.py` | sign-in flow: link, loopback callback, code exchange, key extraction, keyring save, `/theos` verify |
| `deepai-proxy.py` | the proxy: catalogue honesty, session LRU, head-preserving trim, calibration, `X-Context-Guard` |
| `deepai-model-filter.py` | thin passthrough that trims the model catalogue the app renders |
| `launch-deepai-filtered.ps1` | launches DeepAI Code pointed at the local filter |
| `test_context_guard.py` | 18 assertions: trim, LRU restore, tool-pair integrity, calibration gating |
| `smoke_guard.py` | live calibration run against a running proxy |
| `conv_test.py` | proves the upstream lane is stateless |
| `probe_cliff.py` | head/mid/tail canary bisect across prompt sizes |
| `probe_sys.py` | SYSTEM/HEAD/MID/TAIL canaries with answer-validity checking |
| `probe_context.py` | earlier prompt-acceptance probe |
| `distinct.py` | behavioural distinctness signature per model id |
| `keycheck.py` | per-key liveness and lane health |
| `theos_balance.py` | reads the Theos balance for the configured key |
| `asar_extract.py` | minimal ASAR reader used to unpack the desktop app |
| `inspect_app_models.py` | per-model attributable turn sizes from the app database |
| `inspect_app_runs.py` | provider turns and model selection per run |
| `inspect_app_meter*.py` | the app's recorded context-meter history |
| `inspect_session_store.py` | one example of each `.jsonl` record type |
| `inspect_session_sizes.py` | per-session role/character/token totals |
| `inspect_session_totals.py` | machine-wide store totals |

---

## Verifying it

```powershell
python -m py_compile deepai-proxy.py
python test_context_guard.py            # 18/18
python deepai-proxy.py --port 8799      # then, in another shell:
python smoke_guard.py 8799
```

A healthy live run looks like:

```
ctx=40000 out=8192
X-Context-Guard: restored=0 trimmed=2015 tokens=39974 ratio=0.66 cap=40000
```

`restored` = messages replayed from the LRU, `trimmed` = messages dropped to fit
the cap, `ratio` = the learned estimator correction, `tokens` = the estimate
actually forwarded.

**Probe trap, recorded because it costs hours:** a probe using a small
`max_tokens` against a reasoning model returns empty `content` and every canary
reads `LOST`. Use `max_tokens >= 500` (1500 for reasoning models) and throw away
rows with `finish_reason: length` and an empty body.

---

## What the app does that the model doesn't

`%USERPROFILE%\.deepai-code\userdata\deepai\pi\deepai\sessions\<encoded-cwd>\<timestamp>_<id>.jsonl`

Append-only event log, one JSON object per line, nothing ever rewritten. Record
types: `session`, `model_change`, `thinking_level_change`, `session_info`,
`message`, `context_edit`.

`parentId` on every record makes the file a tree, so retries, edits and forks
each become a branch and the old branch stays on disk. `buildSessionPath` →
`buildSessionProjection` → `projection.messages` reconstructs the selected
branch and sends **the whole thing, every turn**.

`context_edit` is the only removal mechanism (`targetId` excluded,
`replacement` optionally substitutes a summary) — and across a whole machine:
**9 sessions, 804 messages, 4.22 MB, ~936,128 tokens persisted, 4 context_edits
total.** The largest single session holds ~564,737 tokens of stored history.

Compaction exists in the installed bundle but cannot fire: the threshold is
`contextWindow - 16384` = **303,616** while `usedTokens` is a local
character-based estimate of the *stored* text, never fed back from the
provider's `prompt_tokens`. It fired zero times across 62 audited turns.

The only lever is `contextWindow` in `models.json` — set it to `54000` with
`maxTokens: 8192` and compaction fires at 37,616, below the real knee. Note that
`models.json` is rewritten on catalogue refresh, so that is a stopgap; the
durable fix is patching `decodeDeepAIModels` in `server.asar`.

---

## Summary

| claim | verdict |
|---|---|
| 320,000-token context | printed in the catalogue, not delivered |
| real delivered ceiling | ~43,500 prompt tokens (drifts to ~49.5k) |
| applies to premier models too | yes — identical wall |
| what gets cut | the FRONT, system message included |
| does the app forget? | no — it stores every turn and replays it |
| does the model receive everything? | no — past the ceiling it receives the tail only |
| can the app's own compaction save it? | it exists, and never fires |

The app's memory is a disk. The model's window is 43k. The number on the box is
neither.

---

## Not included

Credentials, API keys, auth tokens, captured traffic, and the unrelated projects
that share the same working folder (worker deployment, the turnstile solver, the
studio, the video pipeline) are all excluded via `.gitignore`. Nothing in this
repository authenticates as anyone: keys are read from the environment or a
local keyring file at runtime.
