#!/bin/bash
set -euo pipefail

if [ $# -lt 1 ]; then
	echo "Usage: $0 <postfix>"
	exit 1
fi

postfix="$1"

# Base: clean upstream image. Output: distinct _<postfix> tag with the overlay
# (X-Forwarded-Proto fix + CA) applied. Build context is the repo root.
BASE_IMAGE="uchimera.azurecr.io/mcpgateway/auth-server:1.29.0"
OUTPUT_IMAGE="uchimera.azurecr.io/mcpgateway/auth-server:1.29.0_${postfix}"

docker build -f docker/Dockerfile.auth.custom \
	--build-arg REGISTRY_BASE_IMAGE="${BASE_IMAGE}" \
	-t "${OUTPUT_IMAGE}" .

echo "Built ${OUTPUT_IMAGE}"

