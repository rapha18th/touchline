#!/usr/bin/env bash
# Deploy Touchline to Azure Container Apps: build in ACR, run with a managed identity that signs
# in to the Foundry models keylessly. Idempotent: rerun to roll out a new image.
#
#   bash infra/deploy.sh            # uses the defaults below
set -euo pipefail

RG=${RG:-rg-touchline}
LOC=${LOC:-swedencentral}
ACR=${ACR:-touchlineacrrm}
ENV_NAME=${ENV_NAME:-touchline-env}
APP=${APP:-touchline}
AI=${AI:-touchline-ai-rm}
TAG=${TAG:-$(git rev-parse --short HEAD)}

az acr show -n "$ACR" -g "$RG" >/dev/null 2>&1 || az acr create -n "$ACR" -g "$RG" -l "$LOC" --sku Basic --output none
az acr build -r "$ACR" -t "touchline:$TAG" . --output none
IMAGE="$ACR.azurecr.io/touchline:$TAG"

az containerapp env show -n "$ENV_NAME" -g "$RG" >/dev/null 2>&1 || \
  az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOC" --output none

if ! az containerapp show -n "$APP" -g "$RG" >/dev/null 2>&1; then
  az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" \
    --image mcr.microsoft.com/k8se/quickstart:latest --system-assigned \
    --ingress external --target-port 8080 --cpu 1 --memory 2Gi --min-replicas 0 --max-replicas 2 --output none
fi
PRINCIPAL=$(az containerapp show -n "$APP" -g "$RG" --query identity.principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)
AI_ID=$(az cognitiveservices account show -n "$AI" -g "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" --scope "$AI_ID" --output none 2>/dev/null || true
az containerapp registry set -n "$APP" -g "$RG" --server "$ACR.azurecr.io" --identity system --output none
az containerapp update -n "$APP" -g "$RG" --image "$IMAGE" \
  --set-env-vars TOUCHLINE_AI_ENDPOINT="https://$AI.openai.azure.com/" TOUCHLINE_CREW=1 --output none
echo "https://$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
