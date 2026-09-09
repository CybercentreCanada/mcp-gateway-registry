#!/bin/bash
set -e

REGION="us-east-1"
ACCOUNT_ID="128755427449"

# List of images to push
IMAGES=(
  "mcpgateway/registry:latest"
  "mcpgateway/currenttime:latest"
  "mcpgateway/mcpgw:latest"
  "mcpgateway/realserverfaketools:latest"
  "mcpgateway/flight-booking-agent:latest"
  "mcpgateway/travel-assistant-agent:latest"
)

echo "Logging into ECR..."
aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

for IMAGE in "${IMAGES[@]}"; do
  REPO_NAME=$(echo $IMAGE | cut -d'/' -f2 | cut -d':' -f1)
  TAG=$(echo $IMAGE | cut -d':' -f2)

  echo ""
  echo "========================================="
  echo "Processing: $IMAGE"
  echo "========================================="

  echo "Creating ECR repository: ${REPO_NAME}..."
  aws ecr create-repository --repository-name ${REPO_NAME} --region ${REGION} 2>/dev/null || echo "Repository already exists"

  echo "Pulling image (AMD64)..."
  docker pull --platform linux/amd64 ${IMAGE}

  echo "Tagging for ECR..."
  docker tag ${IMAGE} ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:${TAG}

  echo "Pushing to ECR..."
  docker push ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:${TAG}

  echo "✅ Done: ${REPO_NAME}:${TAG}"
done

echo ""
echo "========================================="
echo "✅ All images pushed to ECR!"
echo "========================================="
echo ""
echo "Update terraform.tfvars with ECR URIs:"
echo "registry_image_uri = \"${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/registry:latest\""
echo "currenttime_image_uri = \"${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/currenttime:latest\""
echo "mcpgw_image_uri = \"${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/mcpgw:latest\""
echo "realserverfaketools_image_uri = \"${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/realserverfaketools:latest\""
echo "flight_booking_agent_image_uri = \"${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/flight-booking-agent:latest\""
echo "travel_assistant_agent_image_uri = \"${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/travel-assistant-agent:latest\""
echo ""
echo "Then run: terraform apply -auto-approve"
