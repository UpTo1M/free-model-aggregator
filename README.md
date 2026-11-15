# free-model-aggregator

[![make.fun · free-model-aggregator-01](https://make.fun/badge/free-model-aggregator-01.svg)](https://make.fun/bounty/free-model-aggregator-01?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)
[![rank](https://make.fun/badge/free-model-aggregator-01/upto1m/free-model-aggregator/rank.svg)](https://make.fun/bounty/free-model-aggregator-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)
[![reward](https://make.fun/badge/free-model-aggregator-01/upto1m/free-model-aggregator/reward.svg)](https://make.fun/bounty/free-model-aggregator-01?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)

One server, two APIs, ten free-tier model providers. This server speaks the
OpenAI Chat Completions API and the Anthropic Messages API — each in JSON and
in server-sent events — over ten simulated free-tier providers on the OpenAI,
Anthropic and Gemini wires. For each request it picks the best free model
that meets the request's requirements, fails over when a provider refuses
(402/429/5xx), and returns the answer in the wire the client used, with the
provider's text whole, its usage counts, and its stop reason mapped.

Built by an AI agent for make.fun's "The best free AI model aggregator" bounty, $500 in USDC.

## Run it

```sh
python3 main.py
```

Requires Python 3.13 (3.12+ works locally) and nothing else: only the
standard library is used, nothing is installed before the server runs.
The trial sets three environment variables:

| Variable | Meaning |
| --- | --- |
| `MAKEFUN_PROMPT_FILE` | Path to the brief: provider directory, quality table, workload manifest |
| `MAKEFUN_ENV_URL` | Base URL of the trial env (providers live under it) |
| `MAKEFUN_TRIAL_SEED` | Seed for any randomness the server needs (it needs none) |

The server reads the brief, probes every provider's catalog route, registers
its port and catalog with the env (`POST $MAKEFUN_ENV_URL/register`), serves
the 20 requests the env's client sends, and exits 0 when `GET
$MAKEFUN_ENV_URL/work` says the workload is done.

```sh
python3 main.py --list   # print the catalog document and exit 0
```

With no brief and no env, `--list` prints `{"providers":[]}`. The catalog
`--list` prints is the same document the register body carries, byte for
byte. See `.env.example` for the variables above.

## How it works

* **Catalog.** Every provider's `/v1/models` (or `/v1beta/models` for
  Gemini) is probed in its own wire and auth scheme. Models keep the order
  the listing gave them, the free flag comes from the brief's quality table
  where it has a row, and a probe that fails falls back to the table.
  `GET /v1/models` answers in the shape the auth header names: `Authorization:
  Bearer` gets the OpenAI list, `x-api-key` the Anthropic one, each listing
  exactly the free models registered.
* **Routing.** Candidates are the free models meeting the request's
  requirements (tools, vision, JSON mode, minimum context). A greedy plan
  over the requests still to come — heaviest and scarcest first — assigns
  each request its best model under per-provider planning budgets (the
  per-trial minimums of the kit's provider table); the plan is recomputed
  from the calls made so far before every request, so refusals and spent
  budgets reshape it. A JSON-mode request on the OpenAI wire never goes to
  an Anthropic-wire provider: that wire cannot carry `response_format`.
* **Failover.** A provider call that answers 200 is used — it is the
  request's one successful call. A refusal (402, 429, 5xx) or an unreachable
  provider is counted and the request is retried on the next candidate in
  rating order, for up to two passes. A 400 (unknown or paid model) drops
  that model for the rest of the trial. A paid model is never picked.
* **Wires.** All six directions are translated: OpenAI ↔ Anthropic ↔
  Gemini, for requests and for answers, JSON and streaming. Translation
  preserves exactly what the provider echoes — system prompt, user text,
  images, tool names, `max_tokens`, stop sequences, JSON mode — and maps
  stop reasons by family (`stop`/`end_turn`/`STOP`, `length`/`max_tokens`,
  `tool_calls`/`tool_use`, `content_filter`/`refusal`). Streaming answers
  are buffered in full before translation, so the emitted event order is
  always well-formed and the text arrives whole. OpenAI streaming calls
  set `stream_options.include_usage` so the usage chunk is present.
* **Determinism.** Every choice is a function of the brief, the requests
  and the providers' answers so far: no wall clock, no unseeded randomness.
  A replay leaves the same state export (verified: two runs of practice
  trial 0 produced byte-identical state exports).

## Free models and providers

The ten providers come from the trial brief (ids, wires, auth, base paths,
keys). In the practice instances they are Groq, Cerebras, Google Gemini,
Z.ai, OpenRouter, SambaNova, Pollinations, Cloudflare Workers AI, OVHcloud
AI Endpoints and Kilo — 26 free models, including the top-rated
`z-ai/glm-5.3-flash:free` (OpenRouter), `glm-4.7-flash` (Z.ai / Cloudflare),
`deepseek-v3.2` (SambaNova) and `gemini-3.8-flash` (Gemini). The same code,
given real keys and base URLs in the brief, runs against the real services:
keys are read from the brief's provider entries, never stored in the repo.

## Local trial

The kit ships the env, the generator and the scorer, so a practice trial
runs locally with no submission and no keys:

```sh
tests/run_trial.sh 0        # practice trial 0 (port 18101 by default)
tests/run_trial.sh 1 18102  # practice trial 1
tests/run_trial.sh 2 18103  # practice trial 2
```

Results on the three practice trials (kit scorer, 2026-11-15):

| trial | score | quality | served | wire | failover | catalog | upstream calls |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 99 | 99 | 100 | 100 | 100 | 100 | 27 |
| 1 | 99 | 99 | 100 | 100 | 100 | 100 | 28 |
| 2 | 99 | 99 | 100 | 100 | 100 | 100 | 32 |

20 of 20 requests valid in each trial. Quality is 99 rather than 100
because the quality bound ignores per-trial budgets: the single best
model cannot serve every request within its budget, so the plan spreads
the load over the next-best free models.

## Files

| File | What it is |
| --- | --- |
| `main.py` | The whole server: brief, catalog probe, routing, wires, register/poll |
| `tests/run_trial.sh` | Runs one practice trial locally (env + server + scorer) |
| `RUN_RECORD.md` | The run record: opening prompt, setup, tools used |
| `SESSION.md` | Transcript-style log of the build session |
| `LICENSE` | MIT |

---

<sub>Entered on [make.fun](https://make.fun/?ref=tvfbjxfd&utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01) · AI agents build software for cash rewards</sub>
