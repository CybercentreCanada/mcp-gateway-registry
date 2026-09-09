#!/bin/bash
#
# generate_commentary_headless.sh - fill the commentary hinge without an agent.
#
# This is the headless replacement for the interactive LLM step. It reads the
# commentary manifest produced by `run_report.sh` (augment_with_commentary.py
# extract) and calls `claude -p` to produce commentary.json. The manifest is
# embedded whole in the prompt so the model needs no tools; it only emits JSON.
#
# Usage:
#   generate_commentary_headless.sh <manifest.json> <commentary.json>
#
# Environment overrides:
#   CLAUDE_BIN=...   Path to the claude CLI (default: claude on PATH).
#   MODEL=...        Model to pin (default: the CLI's configured default).

set -euo pipefail

MANIFEST="${1:?usage: generate_commentary_headless.sh <manifest.json> <commentary.json>}"
OUT="${2:?usage: generate_commentary_headless.sh <manifest.json> <commentary.json>}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
PY="/usr/bin/python3"

if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: manifest not found: $MANIFEST" >&2
    exit 1
fi
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    echo "ERROR: claude CLI not found ($CLAUDE_BIN). Set CLAUDE_BIN=..." >&2
    exit 1
fi

# Build the prompt: the manifest already carries `instructions_for_llm` and the
# per-section text. We add a hard wrapper telling the model to emit ONLY the
# JSON object, so parsing is deterministic.
PROMPT="You are generating analyst commentary for a usage report. Below is a JSON
manifest with an \`instructions_for_llm\` field and a \`sections_needing_commentary\`
array. Follow instructions_for_llm exactly. Output ONLY a single JSON object
mapping each section_id to its commentary paragraph (or an empty string to drop
a section). Do not wrap the JSON in markdown fences. Do not print anything before
or after the JSON object.

MANIFEST:
$(cat "$MANIFEST")"

MODEL_FLAG=()
if [ -n "${MODEL:-}" ]; then
    MODEL_FLAG=(--model "$MODEL")
fi

echo ">>> Generating commentary via $CLAUDE_BIN -p ..." >&2
RAW="$("$CLAUDE_BIN" -p "${MODEL_FLAG[@]}" "$PROMPT")"

# Strip optional markdown fences, then validate it is a JSON object before
# writing. If the model returned prose or invalid JSON, fail loudly rather than
# writing a broken commentary file.
printf '%s' "$RAW" | "$PY" -c "
import json, re, sys
raw = sys.stdin.read().strip()
# Remove a leading/trailing markdown fence if present.
raw = re.sub(r'^\`\`\`[a-zA-Z]*\n', '', raw)
raw = re.sub(r'\n\`\`\`\$', '', raw).strip()
# If there is leading/trailing prose, grab the outermost {...}.
if not raw.startswith('{'):
    start = raw.find('{')
    end = raw.rfind('}')
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
obj = json.loads(raw)
if not isinstance(obj, dict):
    raise SystemExit('commentary output is not a JSON object')
json.dump(obj, open('$OUT', 'w'), indent=2)
print('commentary sections written: %d' % len(obj), file=sys.stderr)
"

echo ">>> Wrote $OUT" >&2
