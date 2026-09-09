#!/bin/bash
#
# finish_report.sh - Half B of the usage-report pipeline (after commentary).
#
# Runs: apply the agent-written commentary.json into the rendered markdown ->
# convert to a self-contained HTML file with pandoc.
#
# Precondition: run_report.sh has produced the markdown + commentary-manifest,
# and the agent has written commentary.json into the dated subfolder.
#
# Usage:
#   finish_report.sh [YYYY-MM-DD] [OUTPUT_DIR]
#
#   YYYY-MM-DD   Report date (default: today, UTC).
#   OUTPUT_DIR   Base reports dir (default: .scratchpad/usage-reports).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

REPORT_DATE="${1:-$(date -u +%F)}"
OUTPUT_DIR="${2:-.scratchpad/usage-reports}"
PY="/usr/bin/python3"

DATE_DIR="$OUTPUT_DIR/$REPORT_DATE"
MD="$DATE_DIR/ai-registry-usage-report-$REPORT_DATE.md"
COMMENTARY="$DATE_DIR/commentary.json"
HTML_NAME="ai-registry-usage-report-$REPORT_DATE.html"

cd "$REPO_ROOT"

echo "=============================================================="
echo "Usage report - Half B (finish_report.sh)"
echo "  Report date:  $REPORT_DATE"
echo "  Markdown:     $MD"
echo "  Commentary:   $COMMENTARY"
echo "=============================================================="

if [ ! -f "$MD" ]; then
    echo "ERROR: rendered markdown not found: $MD" >&2
    echo "Did run_report.sh complete? Run it first." >&2
    exit 1
fi
if [ ! -f "$COMMENTARY" ]; then
    echo "ERROR: commentary.json not found: $COMMENTARY" >&2
    echo "The agent must write commentary before finishing. See commentary-manifest.json." >&2
    exit 1
fi


_apply_commentary() {
    echo ">>> Applying commentary into the markdown..."
    "$PY" "$SCRIPT_DIR/augment_with_commentary.py" apply \
        --md "$MD" \
        --commentary "$COMMENTARY"
}


_ensure_pandoc() {
    if ! command -v pandoc >/dev/null 2>&1; then
        echo ">>> Installing pandoc..."
        sudo apt-get install -y pandoc
    fi
}


_generate_html() {
    echo ">>> Generating self-contained HTML..."
    # Run from DATE_DIR so relative image paths in the markdown resolve.
    ( cd "$DATE_DIR" && pandoc "ai-registry-usage-report-$REPORT_DATE.md" \
        -o "$HTML_NAME" \
        --embed-resources --standalone \
        --css="$SCRIPT_DIR/report-style.css" \
        --metadata title="AI Registry - Usage Report $REPORT_DATE" )
    echo ">>> Wrote $DATE_DIR/$HTML_NAME"
}


main() {
    _apply_commentary
    _ensure_pandoc
    _generate_html

    echo "=============================================================="
    echo "Report complete:"
    echo "  Markdown: $MD"
    echo "  HTML:     $DATE_DIR/$HTML_NAME"
    echo "=============================================================="
}

main "$@"
