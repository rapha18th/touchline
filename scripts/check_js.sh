#!/usr/bin/env bash
# Syntax-check every web module as an ES module.
#
#   bash scripts/check_js.sh            # checks web/**/*.js
#   bash scripts/check_js.sh some/dir   # or any other folder
#
# `node --check file.js` treats a .js file outside a "type": "module" package as ambiguous, and on
# Node 22 and 24 a broken file that holds an import passes silently. Each file is copied to .mjs
# first, so Node parses it as the module the browser loads.
set -uo pipefail
ROOT=${1:-web}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
status=0 n=0
while IFS= read -r f; do
  copy="$TMP/$(echo "$f" | tr '/\\:' '___').mjs"
  cp "$f" "$copy"
  n=$((n + 1))
  if out=$(node --check "$copy" 2>&1); then
    echo "ok    $f"
  else
    status=1
    line=$(echo "$out" | sed -n 's/.*\.mjs:\([0-9][0-9]*\).*/\1/p' | head -1)
    msg=$(echo "$out" | grep -m1 -E '^[A-Za-z]*Error' || echo "syntax error")
    echo "FAIL  $f:${line:-?} $msg"
    # One annotation per file when this runs on GitHub Actions.
    [ -n "${GITHUB_ACTIONS:-}" ] && echo "::error file=$f,line=${line:-1}::$msg"
  fi
done < <(find "$ROOT" -name '*.js' -not -path '*/node_modules/*' | sort)
[ "$n" -gt 0 ] || { echo "no .js files under $ROOT"; exit 1; }
echo "$n files checked"
exit $status
