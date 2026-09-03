#!/usr/bin/env bash
set -euo pipefail
exec >/dev/null 2>&1

title=${1:?usage: scripts/fire_pr.sh TITLE [BODY]}
body=${2:-}
branch=$(git branch --show-current)
base=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)

if [[ -z "$branch" || "$branch" == "$base" ]]; then
  exit 1
fi

git push --quiet --set-upstream origin "$branch"
pr_url=$(gh pr create --base "$base" --head "$branch" --title "$title" --body "$body")
gh pr merge "$pr_url" --merge
