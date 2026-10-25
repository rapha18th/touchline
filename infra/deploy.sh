#!/usr/bin/env bash
# Deploy Touchline to Azure Container Apps: build in ACR, run with a managed identity that signs
# in to the Foundry models keylessly. Idempotent: rerun to roll out a new image.
#
#   bash infra/deploy.sh                 # uses the defaults below
#   SKIP_BUILD=1 TAG=567851e bash infra/deploy.sh   # roll out an image already in ACR
#   STAGE_ARGS="--matches D:/matches" bash infra/deploy.sh
#
# The build context is staged by infra/stage.py: only what the Dockerfile copies, and only the
# showcase matches. Every Azure call that can drop on a flaky network is retried, and long waits are
# short polls, never one long-held connection.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash would rewrite /subscriptions/... scopes into file paths
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."

RG=${RG:-rg-touchline}
LOC=${LOC:-swedencentral}
ACR=${ACR:-touchlineacrrm}
ENV_NAME=${ENV_NAME:-touchline-env}
APP=${APP:-touchline}
AI=${AI:-touchline-ai-rm}
TAG=${TAG:-$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- 2>/dev/null || echo -dirty)}
SUFFIX=${SUFFIX:-r$TAG-$(date +%m%d%H%M)}   # unique per rollout, so a rerun of one commit is not refused
RETRIES=${RETRIES:-5}
POLL_S=${POLL_S:-15}
POLLS=${POLLS:-80}                         # 80 polls of 15 s: 20 minutes

retry() {  # retry CMD...: rerun a command that failed, with a growing pause, up to $RETRIES tries
  local n=1
  until "$@"; do
    if [ "$n" -ge "$RETRIES" ]; then echo "gave up after $n tries: $1 $2 $3" >&2; return 1; fi
    echo "try $n of $RETRIES failed ($1 $2 $3); again in $((n * 10)) s" >&2
    sleep $((n * 10)); n=$((n + 1))
  done
}

poll() {  # poll LABEL REGEX CMD...: run a short CMD every $POLL_S s until its output matches REGEX
  local label=$1 re=$2 i=0 s=""
  shift 2
  while [ "$i" -lt "$POLLS" ]; do
    if s=$("$@" 2>/dev/null) && [[ "$s" =~ $re ]]; then echo "$s"; return 0; fi
    i=$((i + 1)); sleep "$POLL_S"
  done
  echo "$label: no final state after $i polls (last: ${s:-no answer})" >&2
  return 1
}

python_bin() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for c in .venv/Scripts/python.exe .venv/bin/python python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo "no python found; set PYTHON" >&2; return 1
}

retry az acr show -n "$ACR" -g "$RG" --output none 2>/dev/null || az acr create -n "$ACR" -g "$RG" -l "$LOC" --sku Basic --output none
if [ -z "${SKIP_BUILD:-}" ]; then
  # shellcheck disable=SC2086  # STAGE_ARGS is a list of flags
  STAGE=$("$(python_bin)" infra/stage.py ${STAGE_ARGS:-})
  echo "build context $STAGE"
  # Queue the build without streaming its log (the Windows console cannot print every glyph pip
  # emits, and a held log stream drops). The upload is the step that fails on a flaky link: retry
  # the whole call. A retry after a lost reply can queue a second build of the same tag; harmless.
  RUN=$(retry az acr build -r "$ACR" -t "touchline:$TAG" "$STAGE" --no-wait --query runId -o tsv)
  echo "acr build run $RUN"
  s=$(poll build '^(Succeeded|Failed|Canceled|Error|Timeout)$' \
        az acr task show-run -r "$ACR" --run-id "$RUN" --query status -o tsv)
  echo "build $s"
  if [ "$s" != Succeeded ]; then echo "log: az acr task logs -r $ACR --run-id $RUN" >&2; exit 1; fi
fi
IMAGE="$ACR.azurecr.io/touchline:$TAG"

retry az containerapp env show -n "$ENV_NAME" -g "$RG" --output none 2>/dev/null || \
  az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOC" --output none

if ! retry az containerapp show -n "$APP" -g "$RG" --output none 2>/dev/null; then
  az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" \
    --image mcr.microsoft.com/k8se/quickstart:latest --system-assigned \
    --ingress external --target-port 8080 --cpu 1 --memory 2Gi --min-replicas 0 --max-replicas 2 --output none
fi
PRINCIPAL=$(retry az containerapp show -n "$APP" -g "$RG" --query identity.principalId -o tsv)
ACR_ID=$(retry az acr show -n "$ACR" -g "$RG" --query id -o tsv)
AI_ID=$(retry az cognitiveservices account show -n "$AI" -g "$RG" --query id -o tsv)
retry az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" --output none
retry az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" --scope "$AI_ID" --output none
retry az containerapp registry set -n "$APP" -g "$RG" --server "$ACR.azurecr.io" --identity system --output none
retry az containerapp update -n "$APP" -g "$RG" --image "$IMAGE" --revision-suffix "$SUFFIX" --no-wait \
  --set-env-vars TOUCHLINE_AI_ENDPOINT="https://$AI.openai.azure.com/" TOUCHLINE_CREW=1 --output none
# Wait for the new revision itself: right after the update the app can still report the old
# revision's Succeeded state.
REV="$APP--$SUFFIX"
s=$(poll rollout "^(Succeeded $REV|Failed .*)$" \
      az containerapp show -n "$APP" -g "$RG" --query "join(' ', [properties.provisioningState, properties.latestRevisionName])" -o tsv)
echo "rollout $s"
[[ "$s" == Succeeded* ]] || exit 1
URL="https://$(retry az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
# The app scales to zero: the first request wakes a replica.
retry curl -fsS --max-time 90 "$URL/healthz" && echo
echo "$URL"
