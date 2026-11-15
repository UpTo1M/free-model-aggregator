#!/usr/bin/env bash
# Run one practice trial of free-model-aggregator-01 against the kit's own
# env server and scorer, locally. No submission, no network beyond the kit
# download (once), no keys.
#
#   tests/run_trial.sh [trial] [port]
#
# trial defaults to 0, port to 18101. KIT_DIR overrides where the kit lives
# (default: /tmp/free-model-aggregator-01-kit); if it is missing the script
# downloads it from make.fun. Needs node (env + scorer) and python3.
set -euo pipefail

TRIAL="${1:-0}"
PORT="${2:-18101}"
KIT_DIR="${KIT_DIR:-/tmp/free-model-aggregator-01-kit}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ ! -f "$KIT_DIR/program/env/server.mjs" ]; then
  echo "downloading the kit to $KIT_DIR ..."
  mkdir -p "$KIT_DIR"
  curl -sL -o "$WORK/kit.tar.gz" "https://make.fun/c/free-model-aggregator-01/kit"
  tar xzf "$WORK/kit.tar.gz" -C "$KIT_DIR" --strip-components=1
fi

# The trial seed is the instance seed from rules/instance.yaml, practice trial $TRIAL.
SEED="$(grep -A3 "t: $TRIAL" "$KIT_DIR/rules/instance.yaml" | grep 'seed:' | head -1 | sed 's/.*seed: *"//; s/"//')"

mkdir -p "$WORK/data" "$WORK/state"
cp "$KIT_DIR/practice/trial-$TRIAL/input/env/providers.json" \
   "$KIT_DIR/practice/trial-$TRIAL/input/env/workload.json" "$WORK/data/"

MAKEFUN_ENV_DATA_DIR="$WORK/data" MAKEFUN_STATE_DIR="$WORK/state" \
  MAKEFUN_ENV_PORT="$PORT" node "$KIT_DIR/program/env/server.mjs" \
  > "$WORK/env.log" 2>&1 &
ENV_PID=$!
for _ in $(seq 1 50); do
  [ -f "$WORK/state/ready" ] && break
  sleep 0.1
done

MAKEFUN_ENV_URL="http://127.0.0.1:$PORT" \
  MAKEFUN_PROMPT_FILE="$KIT_DIR/practice/trial-$TRIAL/input/prompt.txt" \
  MAKEFUN_TRIAL_SEED="$SEED" \
  timeout 150 python3 "$ROOT/main.py" > "$WORK/bot.log" 2>&1 || {
    echo "bot exited $?"
    cat "$WORK/bot.log"
    kill -TERM "$ENV_PID" 2>/dev/null || true
    exit 1
  }

kill -TERM "$ENV_PID" 2>/dev/null || true
sleep 1
MAKEFUN_OUTPUT_DIR="$WORK/state/export" \
  MAKEFUN_REFERENCE_DIR="$KIT_DIR/practice/trial-$TRIAL/reference" \
  MAKEFUN_SCORES_DIR="$WORK/scores" \
  node "$KIT_DIR/program/scorer/score.mjs"
