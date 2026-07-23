#!/bin/bash
#
# run_report_headless.sh - full usage-report pipeline in one command, no agent.
#
# Chains: run_report.sh (Half A) -> generate_commentary_headless.sh (claude -p
# fills the commentary) -> finish_report.sh (Half B). This is the cron
# entrypoint for the "every morning" schedule.
#
# Usage:
#   run_report_headless.sh [YYYY-MM-DD] [OUTPUT_DIR]
#
#   YYYY-MM-DD   Report date (default: today, UTC).
#   OUTPUT_DIR   Base reports dir (default: .scratchpad/usage-reports).
#
# Environment overrides are passed through to the sub-scripts:
#   FORCE_EXPORT, BASTION_IP, SSH_KEY   -> run_report.sh
#   CLAUDE_BIN, MODEL                   -> generate_commentary_headless.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

REPORT_DATE="${1:-$(date -u +%F)}"
OUTPUT_DIR="${2:-.scratchpad/usage-reports}"
DATE_DIR="$OUTPUT_DIR/$REPORT_DATE"
MANIFEST="$DATE_DIR/commentary-manifest.json"
COMMENTARY="$DATE_DIR/commentary.json"

cd "$REPO_ROOT"

echo "=============================================================="
echo "Usage report - HEADLESS full run"
echo "  Report date: $REPORT_DATE"
echo "  Output dir:  $OUTPUT_DIR"
echo "  Started:     $(date -u +'%F %T UTC')"
echo "=============================================================="

# Half A: everything up to the commentary manifest.
"$SCRIPT_DIR/run_report.sh" "$REPORT_DATE" "$OUTPUT_DIR"

# The hinge: fill commentary.json headlessly.
"$SCRIPT_DIR/generate_commentary_headless.sh" "$MANIFEST" "$COMMENTARY"

# Half B: apply commentary and render HTML.
"$SCRIPT_DIR/finish_report.sh" "$REPORT_DATE" "$OUTPUT_DIR"

echo "=============================================================="
echo "HEADLESS run complete: $(date -u +'%F %T UTC')"
echo "  Report: $DATE_DIR/ai-registry-usage-report-$REPORT_DATE.md"
echo "  HTML:   $DATE_DIR/ai-registry-usage-report-$REPORT_DATE.html"
echo "=============================================================="
