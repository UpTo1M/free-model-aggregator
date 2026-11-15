# Build session log — free-model-aggregator-01

Transcript-style log of the session that built this entry, Sun 15 Nov 2026
(WIB). The agent is a Muse subagent (Muse Spark 1.3) working alone on this
one bounty. Times are approximate, from the session's own clock.

## 21:43 — Task received

Parent agent's handoff: build the make.fun entry for "The best free AI
model aggregator" ($500, deadline Mon 16 Nov 2026 09:30 WIB) for @upto1m.
Work dir `~/workspace/makefun/ai-model-aggregator/`. Do not submit, push or
upload anything; no credentials; no payments; no secrets in files.

## 21:44 — Reading the brief

- `browser.open` on the make.fun homepage's second bounty link (url_id
  5937844181468842886, outlink_idx 1) fetched the bounty page and verified
  the title: "The best free AI model aggregator", $500, ends Mon Nov 16
  2026 02:30 UTC. Short brief: one server, two APIs, ten simulated
  free-tier providers, 20 requests.
- `browser.open` on `https://make.fun/skill.md` read the agent guide
  (v1.5.172): entries are a public repo at one commit plus AI transcripts;
  bundle rules, run-record rules, handed-over sessions take no paid place.
- `curl https://make.fun/api/competitions/free-model-aggregator-01/requirements`
  first returned the wrong competition's document (roblox-game-01) — a
  stale/cached answer; a retry returned the right one
  (free-model-aggregator-01, harness agent/v1). Noted the checks that
  matter for the build: repo created after open, OSI license, no committed
  provider key, entry size caps, README badges, transcript gate
  (min_reproduced 30%, min_file_writes 2, min_assistant_turns 5).
- `curl https://make.fun/api/competitions/free-model-aggregator-01` gave
  the full competition JSON. The `rules` field held the complete spec:
  register protocol, `/work` polling, catalog format, the five scoring
  columns (quality 60, served 15, wire 10, failover 10, catalog 5),
  determinism/replay, prizes ($100/$70/$50/$20×7/$14×10).

## 21:47 — Downloading the kit

- `curl -sL https://make.fun/c/free-model-aggregator-01/kit` → 6.6 MB
  tarball. It contains the starter (`main.ts` + `lib/providers.ts` +
  `lib/wire.ts`), three practice trials (providers/workload/prompt +
  reference catalog/ratings), the env server, the generator, the scorer
  and the CLI. Extracted to `/tmp/kit/free-model-aggregator-01-kit`.

## 21:48–21:52 — Studying the kit (the decisions all come from here)

- Read the starter `main.ts` (397 lines) and its two libs: brief parsing
  (three JSON docs by `schema`), catalog probe per wire, `--list`, register
  and `/work` poll. Noted the starter's deliberate gaps: no rating-based
  routing, no failover, only the OpenAI upstream wire, catalog models
  sorted by id.
- Read practice trial 0's `prompt.txt`: the provider directory (10
  providers, wires, auth, paths, sim keys), the quality table (ratings,
  context, tools/vision/JSON, free) and the workload manifest (20 requests:
  r01–r06 weight 2, r07–r20 weight 1; tools/vision/JSON/min_context
  requirements). Best ratings: 1475 (OpenRouter `z-ai/glm-5.3-flash:free`),
  1470, 1465, 1458.
- Read the env server (1,811 lines) end to end. Key facts learned:
  - Providers echo what they parsed (`ANSWER ECHO{...}`) and the client
    compares the echo field for field: system hash, user hash, tool names,
    max_tokens, stops, image hash, JSON mode. A field rewritten or dropped
    loses the request.
  - A JSON-mode request's echo says `json_mode` only when the upstream
    wire can carry it: OpenAI via `response_format`, Gemini via
    `responseMimeType`, Anthropic never.
  - Exactly one successful provider call per request (`one_provider`
    check): a 200 must be used, never retried.
  - Usage must match the provider's counts exactly (for Anthropic
    streaming, output only); OpenAI streaming usage only arrives when
    `stream_options.include_usage` is set — and no client request sets it.
  - Stop reasons are checked by family, not spelling.
  - Budgets count every call naming a free model, refusals included;
    one provider per trial has an outage; one call at a time per provider
    (zai enforces it with 429 `concurrent_requests`).
  - `GET /v1/models` is asked at the trial's start with both auth headers
    and must list exactly the free model ids registered.
  - Each request must be answered within 30 s; the trial wall is 180 s.
- Read the scorer (552 lines): quality is the weighted rating over the
  budget-ignoring best; wire is the share of all checks passed; failover
  is the share of first-call-failed requests still served; catalog is 4
  checks per provider plus 2 for the model routes — and the free-model
  list is compared **in order** against the sealed table, so the catalog
  must keep the provider listing's order, not alphabetical (the starter's
  sort would fail it).
- Read the generator: budgets are drawn per trial in
  `[calls_min, calls_max]` (groq 9–12, cerebras 5–6, gemini 6–8, zai 4–5,
  openrouter 10–12, sambanova 4–5, pollinations 10–14, cloudflare 7–8,
  ovh 5–6, kilol 12–16), 1–2 fail calls per provider, one outage.
- Read the practice workload bodies: single-turn requests, no multi-turn
  tool conversations; r07 carries `response_format: json_object`;
  r08/r09 ask for JSON in text only; no request sends `stream_options`.

## 21:53 — Design decision

Python 3.13, standard library only, single file `main.py` (the brief
allows `main.py` instead of `main.ts`; Python avoids TypeScript
type-stripping risk and the translators port cleanly). Design:

- Catalog from live probes, listing order kept, quality table for free
  flags/capabilities, ratings-table fallback for a failed probe.
- Routing: candidates = free + meets requirements; JSON-mode OpenAI
  requests never go to Anthropic-wire providers; a greedy plan over the
  remaining requests (heaviest/scarcest first) under per-provider
  planning budgets (the kits' `calls_min` values), recomputed before every
  request; failover in rating order, two passes, 400 drops the model.
- All six wire directions translated for requests and answers, JSON and
  SSE; streams buffered in full, then re-emitted well-formed.

## 21:55 — Writing main.py, chunk 1

Wrote the file header, constants, brief parser (three docs by `schema`,
with the starter's object-scan), provider/catalog probe (per-wire routes,
auth headers, listing parsers, `zero_priced` fallback), capability and
requirement checks, and `build_catalog` (brief order, listing order,
ratings fallback).

## 21:57 — Writing main.py, chunk 2

Appended the translators for requests: text helpers, stop-reason families,
`anthropic_to_openai` (system, messages incl. tool_use/tool_result,
images, tools, tool_choice), `openai_to_anthropic` (system join,
tool_result attachment that preserves the echoed user text),
`openai_to_gemini` and `anthropic_to_gemini` (systemInstruction, contents,
functionDeclarations, generationConfig with `responseMimeType` for JSON
mode; tool messages dropped so they never pollute the echoed user text).

## 21:59 — Writing main.py, chunk 3

Appended response translation: JSON part-extractors and builders for all
three wires, SSE frame parsing and re-emission for all three wires
(OpenAI ↔ Anthropic ↔ Gemini, text plus tool calls), stop mapping by
family, same-wire passthrough, and the upstream call (`urllib`, 10 s
timeout, status 0 on unreachable).

## 22:01 — Writing main.py, chunk 4

Appended routing state (`Router`: candidates, greedy plan, failover with
two passes, per-provider call/refusal counts), the HTTP server
(`ThreadingHTTPServer`, `/v1/models` by auth header, both POST routes),
register with retries and the `/work` poll loop that exits 0 on done.

## 22:03 — First edit to main.py

Spotted and fixed a bug in the failover loop's attempt cap (it compared
the number of distinct pairs tried against twice the sequence length,
which could never trigger; replaced with a counter of attempts made).

## 22:04 — Syntax check and first --list

- `python3 -m py_compile main.py` → OK (1,866 lines).
- `python3 main.py --list` with no brief/env → `{"providers":[]}`, exit 0.

## 22:05 — Trial 0, run 1: score 73 — a real bug found

Started the kit env for practice trial 0 (port 18101) and ran the server
against it. Bot exited 0; env listed 22 free models on both wires.
Scorer: **73** (quality 69, served 70, wire 96, failover 60, catalog
100). Six requests invalid, all Anthropic-client requests routed to
OpenAI-wire providers: `echo_system` / `echo_tools` / `echo_stop` failed
— the provider echoed no system, no tools, no stops.

Root cause: in `upstream_request`, the OpenAI-wire branch passed the
client body through verbatim even for an Anthropic client, so the
Anthropic-shaped system/tools/stops never reached the provider's parser.

## 22:07 — Second edit to main.py, trial 0 run 2: score 99

Fixed `upstream_request`: for an Anthropic client and an OpenAI-wire
provider, translate with `anthropic_to_openai` first (streaming still
forces `stream_options.include_usage`). Re-ran trial 0: **99** (quality
99, served 100, wire 100, failover 100, catalog 100; 20/20 valid, 27
upstream calls).

## 22:09 — Trials 1 and 2: score 99 each

Same procedure, different budgets/fail calls/outages (trial 1: ovh
outage from call 3; trial 2: cloudflare outage from call 6). Trial 1:
**99**, 20/20 valid, 28 calls. Trial 2: **99**, 20/20 valid, 32 calls.

## 22:11 — Determinism check: byte-identical replay

Re-ran trial 0 a second time and compared the state exports:
`cmp` → identical. The replay requirement holds: every choice is a
function of the brief, the requests and the providers' answers.

## 22:13 — --list checks

- With the trial env up, `python3 main.py --list` output matched the
  registered catalog byte for byte.
- With the env unreachable (port 9), `--list` printed the ratings-table
  fallback catalog: 10 providers, 26 free models (the paid
  `anthropic/claude-opus-5.5` is absent — it has no ratings row).

## 22:15 — Third edit to main.py

Refinement: a planned first choice is only tried if it is in the
request's actual candidate set (it could fall out when the body's JSON
mode differs from the plan's assumption). No behaviour change on the
practice trials; avoids one wasted upstream call in the edge case.

## 22:16 — tests/run_trial.sh

Wrote the repo's local trial runner (downloads the kit if missing,
starts the env, runs the server, scores). Fixed two mistakes in its
first draft (dead seed-lookup code, a `pkill` that could match its own
command line). Ran it: `tests/run_trial.sh 0` → **99**, 20/20 valid.

## 22:18 — Docs and license

Wrote `LICENSE` (MIT, copyright Muhammad Alfarisi), `README.md` (badges
for `upto1m/free-model-aggregator`, referral `tvfbjxfd`, run instructions,
design, local trial results), `.env.example` (variable names only, no
secrets), `RUN_RECORD.md` (opening prompt, setup, verification table,
what bos must still do). Removed the `__pycache__` left by the syntax
check so the tree ships clean.

## 22:20 — This file

Wrote `SESSION.md` (this log) as the last deliverable. End of build
session. Nothing was submitted, pushed, uploaded or paid; no key, seed or
password was asked for or written anywhere.
