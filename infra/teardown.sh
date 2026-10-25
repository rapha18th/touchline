#!/usr/bin/env bash
# Remove every Touchline resource from Azure: the resource group (Container App, its environment and
# logs, the registry, the Foundry account and its model deployments, the role assignments scoped to
# them), then purge the soft-deleted Foundry account so its name is free, then delete the GitHub
# deploy identity if docs/DEPLOY.md was followed.
#
#   bash infra/teardown.sh          # list what would be deleted; deletes nothing
#   bash infra/teardown.sh --yes    # list it, then delete it
#
# Without --yes the script only reads. Deletion is queued with --no-wait and followed by short polls,
# because long-held ARM connections drop on some networks.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash would rewrite /subscriptions/... ids into file paths
export PYTHONIOENCODING=utf-8

RGS=${RGS:-rg-touchline}                          # space-separated resource groups to delete
APP_REG=${APP_REG:-touchline-github-deploy}       # Entra app registration of the CD workflow
NAME_HINT=${NAME_HINT:-touchline}                 # soft-deleted accounts and stray groups matching this
RETRIES=${RETRIES:-5}
POLL_S=${POLL_S:-20}
POLLS=${POLLS:-90}                                # 90 polls of 20 s: 30 minutes

YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (the only flag is --yes)" >&2; exit 2 ;;
  esac
done

retry() {
  local n=1
  until "$@"; do
    if [ "$n" -ge "$RETRIES" ]; then echo "gave up after $n tries: $1 $2 $3" >&2; return 1; fi
    echo "try $n of $RETRIES failed ($1 $2 $3); again in $((n * 10)) s" >&2
    sleep $((n * 10)); n=$((n + 1))
  done
}

SUB=$(az account show --query "join(' ', [name, id])" -o tsv) || { echo "sign in first: az login" >&2; exit 1; }
echo "Subscription: $SUB"
echo

found=0
groups=()
for g in $RGS; do
  if [ "$(retry az group exists -n "$g")" != true ]; then
    echo "Resource group $g: not found"
    continue
  fi
  groups+=("$g")
  found=1
  echo "Resource group $g, and everything in it:"
  retry az resource list -g "$g" --query "sort_by([], &type)[].[type, name]" -o tsv | awk -F'\t' '{printf "  %-55s %s\n", $1, $2}'
done

others=$(retry az group list --query "[?contains(name, '$NAME_HINT') && managedBy == null].name" -o tsv | tr -d '\r')
for o in $others; do
  case " $RGS " in *" $o "*) ;; *) echo "Not touched: resource group $o matches '$NAME_HINT' (add it to RGS to delete it)";; esac
done

deleted_accounts() {
  az cognitiveservices account list-deleted --query "[?contains(name, '$NAME_HINT')].[name, location, id]" -o tsv 2>/dev/null | tr -d '\r'
}
soft=$(retry deleted_accounts)
if [ -n "$soft" ]; then
  found=1
  echo "Soft-deleted Foundry accounts to purge:"
  echo "$soft" | awk -F'\t' '{printf "  %s (%s)\n", $1, $2}'
fi

app_ids=$(az ad app list --display-name "$APP_REG" --query "[].appId" -o tsv 2>/dev/null | tr -d '\r' || true)
if [ -n "$app_ids" ]; then
  found=1
  echo "Entra app registration $APP_REG (the CD deploy identity): $app_ids"
fi

echo
if [ "$found" -eq 0 ]; then echo "Nothing to delete."; exit 0; fi
if [ "$YES" -ne 1 ]; then
  echo "Nothing deleted. Rerun with --yes to delete everything listed above."
  exit 0
fi

for g in "${groups[@]}"; do
  echo "Deleting resource group $g (queued)"
  retry az group delete -n "$g" --yes --no-wait
done
for g in "${groups[@]}"; do
  i=0
  until [ "$(az group exists -n "$g" 2>/dev/null | tr -d '\r')" = false ]; do
    i=$((i + 1))
    if [ "$i" -ge "$POLLS" ]; then echo "$g still exists after $i polls; rerun to resume" >&2; exit 1; fi
    sleep "$POLL_S"
  done
  echo "Resource group $g: gone"
done

# Deleting a Foundry (Cognitive Services) account keeps it soft-deleted for 48 hours and holds its name.
soft=$(retry deleted_accounts)
while IFS=$'\t' read -r name loc id; do
  [ -n "$name" ] || continue
  rg=$(echo "$id" | sed -n 's|.*/resourceGroups/\([^/]*\)/.*|\1|p')
  echo "Purging soft-deleted account $name"
  retry az cognitiveservices account purge -n "$name" -l "$loc" -g "$rg"
done <<< "$soft"

for id in $app_ids; do
  echo "Deleting app registration $APP_REG ($id)"
  retry az ad app delete --id "$id"
done

echo "Done. Every listed Touchline resource is deleted."
