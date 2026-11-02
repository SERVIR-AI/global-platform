#!/usr/bin/env bash
# The repository's governance as code: the settings GitHub keeps outside the tree,
# applied idempotently with the GitHub CLI by someone with admin on the repository.
#
# Run it AFTER .github/workflows/ci.yml has completed once on main, because the ruleset
# names that workflow's checks, and a required check that has never reported blocks
# every merge.
#
#   deploy/ci/github-settings.sh                 # SERVIR-AI/global-platform
#   REPO=owner/name deploy/ci/github-settings.sh
#
# What it sets, and why (CI/CD note, Level 1):
#   1. workflow tokens default to read-only; a job that needs more says so in its own
#      permissions block (ci.yml's publish job does)
#   2. actions must be pinned to a full commit SHA (GitHub's hardening guide)
#   3. secret scanning, push protection and Dependabot security updates are on
#   4. a ruleset on the default branch: changes arrive by pull request with one
#      approval, stale approvals are dismissed, the five CI checks are required and
#      must be up to date, history is linear, no force pushes, no deletion.
#      Repository admins may bypass, so a one-person day is never blocked by the
#      gate meant for a team.
set -euo pipefail

REPO="${REPO:-SERVIR-AI/global-platform}"
RULESET_NAME="main: merge gate"

say() { printf '\n== %s\n' "$*"; }

say "1. workflow token: read-only by default"
gh api -X PUT "repos/${REPO}/actions/permissions/workflow" \
  -f default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=false

say "2. actions: a full commit SHA on every uses:"
gh api -X PUT "repos/${REPO}/actions/permissions" \
  -F enabled=true -f allowed_actions=all -F sha_pinning_required=true

say "3. secret scanning, push protection, Dependabot security updates"
gh api -X PATCH "repos/${REPO}" --input - --jq '.security_and_analysis' <<'JSON'
{"security_and_analysis": {
  "secret_scanning": {"status": "enabled"},
  "secret_scanning_push_protection": {"status": "enabled"},
  "dependabot_security_updates": {"status": "enabled"}}}
JSON

say "4. ruleset on the default branch: ${RULESET_NAME}"
# actor_id 5 with actor_type RepositoryRole is the repository admin role.
ruleset=$(cat <<'JSON'
{
  "name": "main: merge gate",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [
    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
  ],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_linear_history"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 1,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false,
      "allowed_merge_methods": ["squash", "rebase"]}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "do_not_enforce_on_create": false,
      "required_status_checks": [
        {"context": "python (3.12)"},
        {"context": "python (3.11)"},
        {"context": "web"},
        {"context": "hygiene"},
        {"context": "image"}]}}
  ]
}
JSON
)
existing=$(gh api "repos/${REPO}/rulesets" --jq ".[] | select(.name == \"${RULESET_NAME}\") | .id" | head -n 1)
if [ -n "${existing}" ]; then
  printf '%s' "${ruleset}" | gh api -X PUT "repos/${REPO}/rulesets/${existing}" --input - --jq '{id, name, enforcement}'
else
  printf '%s' "${ruleset}" | gh api -X POST "repos/${REPO}/rulesets" --input - --jq '{id, name, enforcement}'
fi

say "done: ${REPO}"
gh api "repos/${REPO}" --jq '{default_branch, security_and_analysis}'
