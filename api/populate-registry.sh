#!/bin/bash
# Populate MCP Gateway Registry with example servers and agents
# This script registers all example MCP servers, A2A agents, and configures federation
set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Function to show usage
show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --registry-url <url>     Registry URL (required if REGISTRY_URL env var not set)"
    echo "  --keycloak-url <url>     Keycloak URL (required if KEYCLOAK_URL env var not set)"
    echo "  --aws-region <region>    AWS region (default: us-east-1)"
    echo "  --token-file <path>      Path to existing token file (optional - will generate if not provided)"
    echo "  --help                   Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Using command-line arguments"
    echo "  $0 \\"
    echo "    --registry-url https://registry.us-east-1.example.com \\"
    echo "    --keycloak-url https://kc.us-east-1.example.com \\"
    echo "    --aws-region us-east-1"
    echo ""
    echo "  # Using environment variables"
    echo "  export REGISTRY_URL=https://registry.us-east-1.example.com"
    echo "  export KEYCLOAK_URL=https://kc.us-east-1.example.com"
    echo "  export AWS_REGION=us-east-1"
    echo "  $0"
    echo ""
    echo "  # Using existing token file"
    echo "  $0 \\"
    echo "    --registry-url https://registry.us-east-1.example.com \\"
    echo "    --keycloak-url https://kc.us-east-1.example.com \\"
    echo "    --token-file /path/to/token.json"
    echo ""
}

# Parse command-line arguments
REGISTRY_URL_ARG=""
KEYCLOAK_URL_ARG=""
AWS_REGION_ARG=""
TOKEN_FILE_ARG=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --registry-url)
            REGISTRY_URL_ARG="$2"
            shift 2
            ;;
        --keycloak-url)
            KEYCLOAK_URL_ARG="$2"
            shift 2
            ;;
        --aws-region)
            AWS_REGION_ARG="$2"
            shift 2
            ;;
        --token-file)
            TOKEN_FILE_ARG="$2"
            shift 2
            ;;
        --help)
            show_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option: $1${NC}"
            echo ""
            show_usage
            exit 1
            ;;
    esac
done

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}MCP Gateway Registry Population Script${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Resolve configuration from arguments or environment variables
REGISTRY_URL="${REGISTRY_URL_ARG:-${REGISTRY_URL:-}}"
KEYCLOAK_URL="${KEYCLOAK_URL_ARG:-${KEYCLOAK_URL:-}}"
AWS_REGION="${AWS_REGION_ARG:-${AWS_REGION:-us-east-1}}"
TOKEN_FILE="${TOKEN_FILE_ARG:-${SCRIPT_DIR}/.token}"

# Validate required parameters
if [[ -z "$REGISTRY_URL" ]]; then
    echo -e "${RED}Error: REGISTRY_URL is required${NC}"
    echo ""
    show_usage
    exit 1
fi

if [[ -z "$KEYCLOAK_URL" ]]; then
    echo -e "${RED}Error: KEYCLOAK_URL is required${NC}"
    echo ""
    show_usage
    exit 1
fi

echo -e "${BLUE}Configuration:${NC}"
echo "  Registry URL: $REGISTRY_URL"
echo "  Keycloak URL: $KEYCLOAK_URL"
echo "  AWS Region: $AWS_REGION"
echo "  Token File: $TOKEN_FILE"
echo ""

# Get M2M token if not provided
if [[ -n "$TOKEN_FILE_ARG" && -f "$TOKEN_FILE" ]]; then
    echo -e "${YELLOW}Step 1: Using provided token file...${NC}"
    echo -e "${GREEN}✓ Token file found: $TOKEN_FILE${NC}"
else
    echo -e "${YELLOW}Step 1: Getting M2M authentication token...${NC}"
    "${SCRIPT_DIR}/get-m2m-token.sh" \
      --aws-region "$AWS_REGION" \
      --keycloak-url "$KEYCLOAK_URL" \
      --output-file "$TOKEN_FILE" \
      registry-admin-bot

    if [[ ! -f "$TOKEN_FILE" ]]; then
        echo -e "${RED}Error: Failed to get M2M token${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Token acquired${NC}"
fi
echo ""

# MCP Server configs
SERVERS=(
  "cli/examples/cloudflare-docs-server-config.json"
  "cli/examples/context7-server-config.json"
  "cli/examples/currenttime.json"
  "cli/examples/mcpgw.json"
  "cli/examples/realserverfaketools.json"
)

# A2A Agent configs
AGENTS=(
  "cli/examples/flight_booking_agent_card.json"
  "cli/examples/travel_assistant_agent_card.json"
)

# Register servers
echo -e "${YELLOW}Step 2: Registering MCP Servers...${NC}"
SUCCESS_COUNT=0
FAIL_COUNT=0

for config in "${SERVERS[@]}"; do
  config_path="${REPO_ROOT}/${config}"
  if [[ ! -f "$config_path" ]]; then
    echo -e "${RED}  ✗ Config not found: $config${NC}"
    ((FAIL_COUNT++))
    continue
  fi

  echo -e "${BLUE}  → Registering: $(basename $config)${NC}"
  set +e  # Temporarily disable exit on error
  uv run python "${SCRIPT_DIR}/registry_management.py" \
    --token-file "$TOKEN_FILE" \
    --registry-url "$REGISTRY_URL" \
    --aws-region "$AWS_REGION" \
    --keycloak-url "$KEYCLOAK_URL" \
    register --config "$config_path" --overwrite 2>&1 | grep -q "successfully\|created\|registered\|updated"
  if [ $? -eq 0 ]; then
    echo -e "${GREEN}  ✓ Registered successfully${NC}"
    ((SUCCESS_COUNT++))
  else
    echo -e "${YELLOW}  ⚠ Failed${NC}"
    ((FAIL_COUNT++))
  fi
  set -e  # Re-enable exit on error
done

echo ""
echo -e "${GREEN}Servers: $SUCCESS_COUNT registered, $FAIL_COUNT skipped/failed${NC}"
echo ""

# Register agents
echo -e "${YELLOW}Step 3: Registering A2A Agents...${NC}"
AGENT_SUCCESS=0
AGENT_FAIL=0

for config in "${AGENTS[@]}"; do
  config_path="${REPO_ROOT}/${config}"
  if [[ ! -f "$config_path" ]]; then
    echo -e "${RED}  ✗ Config not found: $config${NC}"
    ((AGENT_FAIL++))
    continue
  fi

  echo -e "${BLUE}  → Registering: $(basename $config)${NC}"
  set +e  # Temporarily disable exit on error
  uv run python "${SCRIPT_DIR}/registry_management.py" \
    --token-file "$TOKEN_FILE" \
    --registry-url "$REGISTRY_URL" \
    --aws-region "$AWS_REGION" \
    --keycloak-url "$KEYCLOAK_URL" \
    agent-register --config "$config_path" 2>&1 | grep -q "successfully\|created\|registered\|updated"
  if [ $? -eq 0 ]; then
    echo -e "${GREEN}  ✓ Registered successfully${NC}"
    ((AGENT_SUCCESS++))
  else
    echo -e "${YELLOW}  ⚠ Failed${NC}"
    ((AGENT_FAIL++))
  fi
  set -e  # Re-enable exit on error
done

echo ""
echo -e "${GREEN}Agents: $AGENT_SUCCESS registered, $AGENT_FAIL skipped/failed${NC}"
echo ""

# Federation configuration
FEDERATION_CONFIG="${REPO_ROOT}/cli/examples/federation-config-example.json"
if [[ -f "$FEDERATION_CONFIG" ]]; then
  echo -e "${YELLOW}Step 4: Configuring Federation with Anthropic Registry...${NC}"

  echo -e "${BLUE}  → Saving federation config...${NC}"
  if uv run python "${SCRIPT_DIR}/registry_management.py" \
    --token-file "$TOKEN_FILE" \
    --registry-url "$REGISTRY_URL" \
    --aws-region "$AWS_REGION" \
    --keycloak-url "$KEYCLOAK_URL" \
    federation-save --config "$FEDERATION_CONFIG" ; then
    echo -e "${GREEN}  ✓ Federation config saved${NC}"
  else
    echo -e "${RED}  ✗ Failed to save federation config${NC}"
  fi

  echo -e "${BLUE}  → Syncing Anthropic federated servers...${NC}"
  if uv run python "${SCRIPT_DIR}/registry_management.py" \
    --token-file "$TOKEN_FILE" \
    --registry-url "$REGISTRY_URL" \
    --aws-region "$AWS_REGION" \
    --keycloak-url "$KEYCLOAK_URL" \
    federation-sync --source anthropic ; then
    echo -e "${GREEN}  ✓ Federated servers imported${NC}"
  else
    echo -e "${RED}  ✗ Failed to sync federated servers${NC}"
  fi
else
  echo -e "${YELLOW}Step 4: Skipping federation (config not found)${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Registry Population Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Show summary commands
echo -e "${BLUE}View registered items:${NC}"
echo ""
echo "  # List all servers"
echo "  uv run python api/registry_management.py \\"
echo "    --token-file $TOKEN_FILE \\"
echo "    --registry-url $REGISTRY_URL \\"
echo "    list"
echo ""
echo "  # List all agents"
echo "  uv run python api/registry_management.py \\"
echo "    --token-file $TOKEN_FILE \\"
echo "    --registry-url $REGISTRY_URL \\"
echo "    agent-list"
echo ""
echo -e "${BLUE}Access the Registry UI:${NC}"
echo "  $REGISTRY_URL"
echo ""
