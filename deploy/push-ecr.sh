#!/usr/bin/env bash
# Build and push PayLens to ECR. Idempotent; safe to re-run.
#   ./deploy/push-ecr.sh [tag]
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
REPO="${ECR_REPO:-paylens}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M)}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"

echo "→ ${URI}:${TAG}"

aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
       --image-scanning-configuration scanOnPush=true >/dev/null

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# linux/amd64 explicitly: building on an M-series Mac otherwise produces an arm64
# image that Fargate X86_64 will pull and then fail to start, with a confusing error.
docker build --platform linux/amd64 -t "${URI}:${TAG}" -t "${URI}:latest" .
docker push "${URI}:${TAG}"
docker push "${URI}:latest"

echo "pushed ${URI}:${TAG}"
