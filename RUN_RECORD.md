# Run record — free-model-aggregator-01

Entry for make.fun's "The best free AI model aggregator" bounty
($500, ends Mon 16 Nov 2026 02:30 UTC / 09:30 WIB).
Built Sun 15 Nov 2026, ~21:43–22:00 WIB. Not submitted.

## Opening prompt (the person I work for, word for word)

The task arrived as a handoff from the parent agent: build the make.fun
bounty entry for "The best free AI model aggregator" ($500, deadline Mon 16
Nov 2026 09:30 WIB) for the make.fun account @upto1m (Muhammad Alfarisi,
GitHub "upto1m", linked Solana wallet Phantom
BW3BNonTDhhEFUwwMKFJ3ZxGVqwi9vAzGtGgxt8Wfys1, profile
https://make.fun/profile, referral https://make.fun/r/tvfbjxfd, inviter
@setyamickala). Work alone on only this bounty; put all deliverables in
~/workspace/makefun/ai-model-aggregator/ (main.ts at root if Node, or main.py
if Python, per the brief), plus README.md, RUN_RECORD.md, SESSION.md and a
MIT LICENSE; build it so it runs locally with free/no-key access where
possible; test the core flow; do NOT submit, push or upload anything; do NOT
ask for any password, seed phrase or private key; do NOT claim the bounty is
entered; do NOT pay anything or make any on-chain transaction; do NOT put
any secret/key in any file.

## System setup

- Harness: Muse (Meta) subagent, model Muse Spark 1.3, running on a Linux
  VM (this machine). No MCP servers were used.
- Provider: Meta (the model serving this session).
- Tools used: `browser.open` (bounty page, skill.md), `curl` (requirements
  and competition JSON, kit download), `node` v24.22.1 (kit env server and
  scorer), `python3` 3.12.3 (the entry; the grader uses Python 3.13),
  standard shell tools.
- Instruction files: none in the work directory. The system prompt is
  withheld: it is the harness's own prompt and is not shared in the repo.
- No secrets were used, stored or written anywhere. The only keys in play
  are the trial's simulated provider keys, which live in the trial brief
  (the kit's practice prompt files) and are never copied into the repo.

## What was built

`main.py` — a single-file Python 3 (standard library only) server. See
README.md for the design and SESSION.md for the step-by-step build log.

## Verification (all local, kit's own env + scorer)

| Check | Result |
| --- | --- |
| `python3 main.py --list` (no brief/env) | `{"providers":[]}`, exit 0 |
| `python3 main.py --list` (trial env up) | byte-identical to the registered catalog |
| `python3 main.py --list` (env unreachable) | ratings-table fallback catalog: 10 providers, 26 free models |
| Practice trial 0 (kit scorer) | **99** — quality 99, served 100, wire 100, failover 100, catalog 100; 20/20 valid, 27 upstream calls |
| Practice trial 1 (kit scorer) | **99** — 20/20 valid, 28 upstream calls |
| Practice trial 2 (kit scorer) | **99** — 20/20 valid, 32 upstream calls |
| Determinism (trial 0 run twice) | state exports byte-identical |
| `tests/run_trial.sh 0` (repo's own runner) | 99, 20/20 valid |

## Still missing before submission (bos must do)

1. **Create the GitHub repo** `upto1m/free-model-aggregator` (created after
   the competition opened, 2026-11-14 02:30 UTC), public, and push this
   directory to it. The README badges name exactly that owner/repo; if the
   repo is named differently, update the three badge URLs in README.md and
   the repo About first.
2. **Set the repo About**: description "`<summary>` Built by an AI agent
   for make.fun's "The best free AI model aggregator" bounty, $500 in
   USDC.", homepage https://make.fun/bounty/free-model-aggregator-01,
   topics `makefun, ai-agents, bounties, code`.
3. **Bundle the transcripts and submit** with the kit CLI before the
   deadline (Mon 16 Nov 2026 02:30 UTC / 09:30 WIB): the entry is the repo
   URL + commit sha + the transcript bundle of the sessions that built it
   (this session and SESSION.md are the record of it). The submission must
   be signed by the Solana wallet linked to the @upto1m account
   (BW3BNonTDhhEFUwwMKFJ3ZxGVqwi9vAzGtGgxt8Wfys1); only bos can do that.
4. Nothing else is missing: no API key is needed (the trial provides the
   simulated keys in its brief), nothing is paid, the MIT LICENSE is in
   place, and the code is ready to run as `python3 main.py`.

## Notes for the submitter

- The entry is Python (`main.py`), not TypeScript: the brief allows either.
  The grader runs `python3 main.py` / `python3 main.py --list`.
- Quality scores 99, not 100, because the quality bound ignores per-trial
  budgets: the single best model cannot serve every request within its
  budget, so the plan spreads the load over the next-best free models.
  This matches the best scores on the public leaderboard (99).
- The `thinking_reproduces` gate needs the transcripts to account for the
  code: `main.py` was written in four chunks and edited three times in this
  session (see SESSION.md), so the file's final content comes from several
  writes, not one.
