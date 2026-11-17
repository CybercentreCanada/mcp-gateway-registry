#!/bin/bash
# Build an overlay of the registry image: the files that differ from the upstream
# tag are copied onto the mirrored upstream image. Backend-only; use the full
# Dockerfile (see deploy/CUSTOM_IMAGE_BUILD.md) when the frontend, nginx templates
# or dependencies changed.
#
# Usage: ./build_registry.sh <image-tag>      e.g. 1.37.0-cccs-base1
# Env:   UPSTREAM_TAG  upstream tag the branch is based on (default 1.37.0)
#        BASE_IMAGE    prebuilt image to overlay (default <ACR>/registry:$UPSTREAM_TAG)
#        IMAGE_REPO    output repository (default uchimera.azurecr.io/mcpgateway/registry)
#        ALLOW_DIRTY=1 build even with uncommitted changes in overlaid paths
#        DRY_RUN=1     stage and list the overlay, do not build
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <image-tag>" >&2
    exit 1
fi

TAG="$1"
UPSTREAM_TAG="${UPSTREAM_TAG:-1.37.0}"
IMAGE_REPO="${IMAGE_REPO:-uchimera.azurecr.io/mcpgateway/registry}"
BASE_IMAGE="${BASE_IMAGE:-${IMAGE_REPO}:${UPSTREAM_TAG}}"
OUTPUT_IMAGE="${IMAGE_REPO}:${TAG}"
OVERLAY_PATHS=(registry auth_server api scripts cli/examples)
STAGE=".overlay"

cd "$(dirname "$0")"

git rev-parse --verify --quiet "${UPSTREAM_TAG}^{commit}" >/dev/null \
    || { echo "Unknown upstream tag ${UPSTREAM_TAG}; run 'git fetch --tags upstream'." >&2; exit 1; }

# Only the paths the image copies can be overlaid. Anything else that differs from
# the upstream tag changes what the image contains, so an overlay would silently
# ship an incomplete build.
unsupported=$(git diff --name-only "${UPSTREAM_TAG}" HEAD | grep -Ev \
    '^(registry|auth_server|api|scripts|cli/examples|charts|docs|tests|terraform|infra|deployment|release-notes)/|^(deployment|\.gitignore|\.gitmodules|build_[a-z]+\.sh|docker/Dockerfile\.[a-z]+\.custom)$|\.md$' \
    || true)
if [ -n "${unsupported}" ]; then
    echo "These changes cannot be carried by an overlay; build the full image instead:" >&2
    while IFS= read -r line; do echo "  ${line}" >&2; done <<<"${unsupported}"
    exit 1
fi

if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git status --porcelain -- "${OVERLAY_PATHS[@]}")" ]; then
    echo "Uncommitted changes in overlaid paths; commit them or set ALLOW_DIRTY=1." >&2
    exit 1
fi

deleted=$(git diff --name-only --diff-filter=D "${UPSTREAM_TAG}" HEAD -- "${OVERLAY_PATHS[@]}")
if [ -n "${deleted}" ]; then
    echo "An overlay cannot delete files; build the full image. Deleted:" >&2
    while IFS= read -r line; do echo "  ${line}" >&2; done <<<"${deleted}"
    exit 1
fi

rm -rf "${STAGE}"
mkdir -p "${STAGE}/app" "${STAGE}/etc"
mapfile -t changed < <(git diff --name-only --diff-filter=AMR "${UPSTREAM_TAG}" HEAD -- "${OVERLAY_PATHS[@]}")
if [ "${#changed[@]}" -eq 0 ]; then
    echo "No overlaid files differ from ${UPSTREAM_TAG}; use the upstream image directly." >&2
    exit 1
fi
for f in "${changed[@]}"; do
    mkdir -p "${STAGE}/app/$(dirname "${f}")"
    cp -p "${f}" "${STAGE}/app/${f}"
done
if [ -f ca-certificates.crt ]; then
    mkdir -p "${STAGE}/etc/ssl/certs"
    cp ca-certificates.crt "${STAGE}/etc/ssl/certs/ca-certificates.crt"
    echo "Including ca-certificates.crt as the image CA bundle."
fi

echo "Overlay of ${BASE_IMAGE} with ${#changed[@]} file(s) from $(git rev-parse --short HEAD):"
printf '  %s\n' "${changed[@]}"

if [ -n "${DRY_RUN:-}" ]; then
    echo "DRY_RUN set: not building."
    exit 0
fi

docker build -f docker/Dockerfile.registry.custom \
    --build-arg REGISTRY_BASE_IMAGE="${BASE_IMAGE}" \
    --build-arg BUILD_VERSION="${TAG}" \
    -t "${OUTPUT_IMAGE}" .
rm -rf "${STAGE}"

echo "Built ${OUTPUT_IMAGE}"
echo "Verify before pushing: docker run --rm --entrypoint printenv ${OUTPUT_IMAGE} BUILD_VERSION"
