#!/bin/bash
# Build an overlay of the auth-server image: the files that differ from the upstream
# tag are copied onto the mirrored upstream image. Dependencies and the entrypoint
# cannot be overlaid; use the full docker/Dockerfile.auth when they changed.
#
# Usage: ./build_auth.sh <image-tag>      e.g. 1.37.0-cccs-base1
# Env:   UPSTREAM_TAG  upstream tag the branch is based on (default 1.37.0)
#        BASE_IMAGE    prebuilt image to overlay (default <ACR>/auth-server:$UPSTREAM_TAG)
#        IMAGE_REPO    output repository (default uchimera.azurecr.io/mcpgateway/auth-server)
#        ALLOW_DIRTY=1 build even with uncommitted changes in overlaid paths
#        DRY_RUN=1     stage and list the overlay, do not build
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <image-tag>" >&2
    exit 1
fi

TAG="$1"
UPSTREAM_TAG="${UPSTREAM_TAG:-1.37.0}"
IMAGE_REPO="${IMAGE_REPO:-uchimera.azurecr.io/mcpgateway/auth-server}"
BASE_IMAGE="${BASE_IMAGE:-${IMAGE_REPO}:${UPSTREAM_TAG}}"
OUTPUT_IMAGE="${IMAGE_REPO}:${TAG}"
# What the auth image copies: auth_server/ -> /app/, registry/ -> /app/registry/.
OVERLAY_PATHS=(auth_server registry)
# Changes here alter the image itself (dependencies, entrypoint, build), not just source.
IMAGE_INPUTS=(auth_server/pyproject.toml auth_server/uv.lock docker/auth-entrypoint.sh docker/Dockerfile.auth)
STAGE=".overlay-auth"
# The upstream auth-server image lacks hvac (declared only in the root pyproject.toml, which the registry
# image installs), so the OBO token cache fails to start there with "No module named 'hvac'" and the auth
# server silently falls back to stateless exchanges. hvac is a pure-Python wheel whose only requirement
# (requests) is already in the image, so it is added as one pinned, hash-checked wheel.
HVAC_VERSION="2.4.0"
HVAC_SHA256="008db5efd8c2f77bd37d2368ea5f713edceae1c65f11fd608393179478649e0f"

cd "$(dirname "$0")"

git rev-parse --verify --quiet "${UPSTREAM_TAG}^{commit}" >/dev/null \
    || { echo "Unknown upstream tag ${UPSTREAM_TAG}; run 'git fetch --tags upstream'." >&2; exit 1; }

changed_inputs=$(git diff --name-only "${UPSTREAM_TAG}" HEAD -- "${IMAGE_INPUTS[@]}")
if [ -n "${changed_inputs}" ]; then
    echo "These changes cannot be carried by an overlay; build the full image instead:" >&2
    while IFS= read -r line; do echo "  ${line}" >&2; done <<<"${changed_inputs}"
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
    dest="${f}"
    case "${f}" in
        auth_server/*) dest="${f#auth_server/}" ;;
    esac
    mkdir -p "${STAGE}/app/$(dirname "${dest}")"
    cp -p "${f}" "${STAGE}/app/${dest}"
done
mkdir -p "${STAGE}/wheels"
wheel_dir=$(mktemp -d)
UV_INDEX_URL="${UV_INDEX_URL:-${PIP_INDEX_URL:-}}" \
    uvx --quiet pip download "hvac==${HVAC_VERSION}" --no-deps --only-binary=:all: -d "${wheel_dir}" >/dev/null
echo "${HVAC_SHA256}  ${wheel_dir}/hvac-${HVAC_VERSION}-py3-none-any.whl" | sha256sum -c - >/dev/null \
    || { echo "hvac ${HVAC_VERSION} wheel does not match the pinned sha256; refusing to build." >&2; rm -rf "${wheel_dir}"; exit 1; }
cp "${wheel_dir}/hvac-${HVAC_VERSION}-py3-none-any.whl" "${STAGE}/wheels/"
rm -rf "${wheel_dir}"
if [ -f ca-certificates.crt ]; then
    mkdir -p "${STAGE}/etc/ssl/certs"
    cp ca-certificates.crt "${STAGE}/etc/ssl/certs/ca-certificates.crt"
    echo "Including ca-certificates.crt as the image CA bundle."
fi

echo "Overlay of ${BASE_IMAGE} with ${#changed[@]} file(s) from $(git rev-parse --short HEAD) and hvac ${HVAC_VERSION}:"
printf '  %s\n' "${changed[@]}"

if [ -n "${DRY_RUN:-}" ]; then
    echo "DRY_RUN set: not building."
    exit 0
fi

docker build -f docker/Dockerfile.auth.custom \
    --build-arg AUTH_BASE_IMAGE="${BASE_IMAGE}" \
    --build-arg BUILD_VERSION="${TAG}" \
    -t "${OUTPUT_IMAGE}" .
rm -rf "${STAGE}"

echo "Built ${OUTPUT_IMAGE}"
echo "Verify before pushing: docker run --rm --entrypoint printenv ${OUTPUT_IMAGE} BUILD_VERSION"
