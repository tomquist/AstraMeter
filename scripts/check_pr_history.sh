#!/usr/bin/env bash
# Reject a PR head that carries commits reachable from main but not develop.

set -euo pipefail

if [ "$#" -ne 9 ]; then
  echo "usage: $0 HEAD DEVELOP MAIN HEAD_REF HEAD_REPO BASE_REPO AUTHOR_LOGIN AUTHOR_TYPE AUTHOR_ASSOCIATION" >&2
  exit 2
fi

head_sha=$1
develop_ref=$2
main_ref=$3
head_ref=$4
head_repo=$5
base_repo=$6
author_login=$7
author_type=$8
author_association=$9

# release.sh intentionally creates this internal PR from release history back
# into develop. Restrict the exemption to the base repository, its exact
# versioned sync-branch convention, and either the release App/Bot or the
# repository owner performing the documented manual recovery path.
trusted_release_author=false
if { [ "$author_type" = "Bot" ] && [[ "$author_login" =~ \[bot\]$ ]]; } ||
  [ "$author_association" = "OWNER" ]; then
  trusted_release_author=true
fi

if [ "$head_repo" = "$base_repo" ] &&
  [ "$trusted_release_author" = true ] &&
  [[ "$head_ref" =~ ^release/v[0-9]+\.[0-9]+\.[0-9]+-develop$ ]]; then
  echo "Internal release sync branch; main ancestry is expected."
  exit 0
fi

main_only=$(mktemp)
head_only=$(mktemp)
overlap=$(mktemp)
trap 'rm -f "$main_only" "$head_only" "$overlap"' EXIT

LC_ALL=C git rev-list "$main_ref" "^$develop_ref" | LC_ALL=C sort >"$main_only"
LC_ALL=C git rev-list "$head_sha" "^$develop_ref" | LC_ALL=C sort >"$head_only"
LC_ALL=C comm -12 "$main_only" "$head_only" >"$overlap"

if [ -s "$overlap" ]; then
  echo "::error::This pull request contains commits from main that are not in develop."
  echo "Rebase the pull request's own commits onto the current develop branch, then force-push with lease."
  echo "Detected main-only commits:"
  while IFS= read -r commit; do
    git show -s --format='  %h %s' "$commit"
  done <"$overlap"
  exit 1
fi

echo "PR history is based on develop without main-only commits."
