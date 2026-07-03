#!/usr/bin/env bash
# push_to_github.sh — commit and push PingMon changes to GitHub
#
# One-time setup:
#   1. If you haven't already, create the repo on GitHub. It's fine if
#      GitHub already added a README, .gitignore, or license — this
#      script merges the remote's existing history in automatically
#      before its first push, so an already-populated repo is fine too.
#   2. Copy its URL, e.g. git@github.com:yourname/pingmon.git
#      (SSH) or https://github.com/yourname/pingmon.git (HTTPS — you'll
#      need a Personal Access Token as the password when it prompts).
#   3. chmod +x push_to_github.sh
#
# Usage:
#   ./push_to_github.sh                       # auto-generated commit message
#   ./push_to_github.sh "Add SNMP UPS pilot"   # custom commit message
#
# The remote only needs to be set once — after the first run it's saved
# in .git/config. To set it non-interactively (e.g. for automation),
# export PINGMON_REMOTE_URL before running instead of typing it at the
# prompt.

set -euo pipefail
cd "$(dirname "$0")"

BRANCH="main"
COMMIT_MSG="${1:-Update PingMon $(date +%Y-%m-%d)}"

# Files that must NEVER be committed, no matter what .gitignore says.
FORBIDDEN_FILES=("pingmon.db" "pingmon.db-wal" "pingmon.db-shm" "pingmon.secret")

echo "== PingMon -> GitHub push =="

# --- 0. Refuse to run if a previous merge was left unresolved -----------------
if [ -f .git/MERGE_HEAD ]; then
    echo "ERROR: a merge from a previous run is still unresolved." >&2
    echo "Fix the conflicts (look for <<<<<<< markers), then run:" >&2
    echo "   git add <resolved files>" >&2
    echo "   git commit" >&2
    echo "   git push -u origin $(git branch --show-current)" >&2
    echo "...before running this script again." >&2
    exit 1
fi

# --- 1. git available? -------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is not installed." >&2
    exit 1
fi

# --- 2. Initialize the repo if this is the first run -------------------------
if [ ! -d .git ]; then
    echo "No git repository here yet — initializing one."
    git init -b "$BRANCH"
else
    BRANCH="$(git branch --show-current)"
fi

# --- 3. Make sure .gitignore exists and covers the sensitive files -----------
if [ ! -f .gitignore ]; then
    echo "ERROR: .gitignore is missing. Re-create it before continuing." >&2
    exit 1
fi
for f in "${FORBIDDEN_FILES[@]}"; do
    grep -qxF "$f" .gitignore 2>/dev/null || echo "$f" >> .gitignore
done

# --- 4. Attach the GitHub remote if not already configured -------------------
if ! git remote get-url origin >/dev/null 2>&1; then
    REMOTE_URL="${PINGMON_REMOTE_URL:-}"
    if [ -z "$REMOTE_URL" ]; then
        read -rp "Enter the GitHub repo URL (e.g. git@github.com:you/pingmon.git): " REMOTE_URL
    fi
    if [ -z "$REMOTE_URL" ]; then
        echo "ERROR: no remote URL given." >&2
        exit 1
    fi
    echo "Adding remote origin -> $REMOTE_URL"
    git remote add origin "$REMOTE_URL"
fi

# --- 5. Stage everything (deletions of old .TXT files included, via .gitignore) ---
git add -A

# --- 6. Safety net: refuse to proceed if a forbidden file got staged ---------
for f in "${FORBIDDEN_FILES[@]}"; do
    if git diff --cached --name-only | grep -qxF "$f"; then
        echo "ERROR: $f is staged for commit — aborting before it reaches GitHub." >&2
        git restore --staged "$f"
        exit 1
    fi
done

# --- 7. Commit (skip cleanly if there's nothing new) --------------------------
if git diff --cached --quiet; then
    echo "Nothing to commit — working tree matches the last commit."
    exit 0
fi

echo
echo "-- Files staged for commit --"
git diff --cached --name-status
echo

git commit -m "$COMMIT_MSG"

# --- 8. Merge in the remote's history first, if it already has any -----------
# Handles the common case of connecting this folder to a GitHub repo that
# was created first (possibly with a README/license/.gitignore already
# added on GitHub's side) — without this, the first push would be rejected
# as non-fast-forward since the two histories don't share a common ancestor.
if git ls-remote --exit-code origin "$BRANCH" >/dev/null 2>&1; then
    echo "Remote already has commits on $BRANCH — merging them in first."
    git fetch origin "$BRANCH"
    if ! git merge --allow-unrelated-histories --no-edit "origin/$BRANCH"; then
        echo >&2
        echo "ERROR: automatic merge failed — likely a real conflict (e.g. both" >&2
        echo "sides have a README.md with different content)." >&2
        echo "Resolve the conflicts shown above, then run:" >&2
        echo "   git add <resolved files>" >&2
        echo "   git commit" >&2
        echo "   git push -u origin $BRANCH" >&2
        exit 1
    fi
fi

# --- 9. Push -------------------------------------------------------------
echo "Pushing to origin/$BRANCH ..."
git push -u origin "$BRANCH"

echo "Done."
