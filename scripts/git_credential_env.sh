#!/bin/sh
# Git credential helper that serves GH_TOKEN from the process environment, or
# (if unset there) this repo's local .env file. Configured with
# `git config --local` only (see setup below) - never touches global git
# config, so it can't affect other repos or other tools' GitHub auth on this
# machine.
#
# One-time setup per clone (run from the repo root):
#   git config --local credential."https://github.com".helper ""
#   git config --local credential."https://github.com".helper "!sh scripts/git_credential_env.sh"

[ "$1" = "get" ] || exit 0

TOKEN="$GH_TOKEN"

if [ -z "$TOKEN" ]; then
    ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
    ENV_FILE="$ROOT/.env"
    if [ -f "$ENV_FILE" ]; then
        TOKEN=$(awk -F= '/^GH_TOKEN=/{sub(/^GH_TOKEN=/,""); print}' "$ENV_FILE" | sed -E 's/[[:space:]]*#.*//; s/[[:space:]]+$//')
    fi
fi

[ -n "$TOKEN" ] || exit 0

echo "username=x-access-token"
echo "password=$TOKEN"
