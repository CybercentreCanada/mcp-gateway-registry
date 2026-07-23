#!/usr/bin/env bash
# Merge an upstream AIGR release tag (or branch) into the current fork branch.
#
# This script is designed for the fork workflow where CybercentreCanada/mcp-gateway-registry
# tracks agentic-community/mcp-gateway-registry upstream releases.  It:
#   1. Ensures the upstream remote is configured.
#   2. Fetches the specified tag or branch from upstream.
#   3. Verifies the ref exists before touching anything.
#   4. Attempts a --no-ff merge into the current branch.
#   5. Reports conflicts for manual resolution.
#
# Usage:
#   ./scripts/merge-upstream.sh <tag-or-branch>
#
# Examples:
#   ./scripts/merge-upstream.sh 1.27.1          # merge release tag
#   ./scripts/merge-upstream.sh main            # merge upstream main
#   ./scripts/merge-upstream.sh 1.28.0          # merge a future release tag
#
# After running, if there are conflicts:
#   1. Inspect each conflicted file:   git diff --name-only --diff-filter=U
#   2. Edit conflicts, keeping our AKS-specific changes
#   3. git add <resolved-files>
#   4. git commit

set -euo pipefail

UPSTREAM_REMOTE="upstream"
UPSTREAM_URL="git@github.com:agentic-community/mcp-gateway-registry.git"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <tag-or-branch>"
    echo "Examples:"
    echo "  $0 1.27.1"
    echo "  $0 main"
    exit 1
fi

REF="$1"
CURRENT_BRANCH=$(git branch --show-current)

echo "Current branch : ${CURRENT_BRANCH}"
echo "Merging ref    : ${REF}"
echo "Upstream       : ${UPSTREAM_URL}"
echo ""

# 1. Ensure upstream remote exists
if ! git remote get-url "${UPSTREAM_REMOTE}" &>/dev/null; then
    echo "[+] Adding remote '${UPSTREAM_REMOTE}' → ${UPSTREAM_URL}"
    git remote add "${UPSTREAM_REMOTE}" "${UPSTREAM_URL}"
fi

# 2. Fetch the specified ref (tag or branch)
echo "[+] Fetching '${REF}' from ${UPSTREAM_REMOTE}..."
git fetch "${UPSTREAM_REMOTE}" "refs/tags/${REF}:refs/tags/${REF}" 2>/dev/null \
    || git fetch "${UPSTREAM_REMOTE}" "${REF}" 2>/dev/null \
    || { echo "[!] Could not fetch '${REF}' from upstream — check that it exists."; exit 1; }

# 3. Resolve the actual commit SHA for the ref
MERGE_SHA=$(git rev-parse "${REF}" 2>/dev/null \
    || git rev-parse "${UPSTREAM_REMOTE}/${REF}" 2>/dev/null \
    || { echo "[!] Could not resolve ref '${REF}'"; exit 1; })

echo "[+] Resolved ${REF} → ${MERGE_SHA:0:12}"
echo ""

# 4. Show what will be merged (commits in upstream not yet in our branch)
BEHIND=$(git log --oneline "${CURRENT_BRANCH}..${MERGE_SHA}" | wc -l | tr -d ' ')
echo "Commits being pulled from upstream: ${BEHIND}"
git log --oneline "${CURRENT_BRANCH}..${MERGE_SHA}" | head -20
if [[ "$BEHIND" -gt 20 ]]; then
    echo "... ($(( BEHIND - 20 )) more)"
fi
echo ""

if [[ "$BEHIND" -eq 0 ]]; then
    echo "[OK] Already up to date — nothing to merge."
    exit 0
fi

# 5. Show files that differ (context before merging)
echo "Files changed in ${REF} vs current branch (chart files only):"
git diff --name-only "${CURRENT_BRANCH}" "${MERGE_SHA}" -- charts/ | grep -v ".tgz" || true
echo ""

# 6. Merge
echo "[+] Merging ${REF} into ${CURRENT_BRANCH}..."
if git merge --no-ff "${MERGE_SHA}" -m "merge: pull upstream ${REF} into ${CURRENT_BRANCH}

Merges agentic-community/mcp-gateway-registry@${REF} (${MERGE_SHA:0:12})
into the fork branch.  AKS-specific changes (ingress nginx, MongoDB config,
openbao-init Python rewrite, registrySubdomain, etc.) are preserved.
Resolve any conflicts keeping the fork's deployment customisations."; then
    echo ""
    echo "[OK] Merge succeeded with no conflicts."
    echo "     Run 'helm dep update charts/mcp-gateway-registry-stack' to"
    echo "     rebuild subchart packages before deploying."
else
    echo ""
    echo "[!] Merge has conflicts.  Resolve them, then:"
    echo "    git add <resolved-files>"
    echo "    git commit"
    echo ""
    echo "Conflicted files:"
    git diff --name-only --diff-filter=U
    echo ""
    echo "Tips for common conflict areas:"
    echo "  charts/mcp-gateway-registry-stack/Chart.yaml"
    echo "    → Keep our mongodb (Bitnami) dependency; accept upstream version bumps"
    echo "  charts/mcp-gateway-registry-stack/values.yaml"
    echo "    → Keep our global.image.registry default (uchimera), operator.enabled,"
    echo "       registrySubdomain; take upstream's new feature defaults"
    echo "  charts/mongodb-configure/templates/configmap.yaml"
    echo "    → Keep our publishSkillEnabled flag + standalone MongoDB fix;"
    echo "       take upstream's new seeding logic"
    echo "  charts/registry/templates/ingress.yaml"
    echo "    → Keep our nginx class-conditional + TLS + registrySubdomain changes"
    echo "  charts/auth-server/templates/secret.yaml"
    echo "    → Keep our registrySubdomain change; take upstream's new fields"
    exit 1
fi
