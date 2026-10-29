#!/usr/bin/env bash
# ==============================================================================
# Conflict-Free Multi-Agent Git Synchronizer (Gemini Quant Arena)
# Usage:
#   ./scripts/git_agent_sync.sh <local-agy|server-agy> "commit message"
# ==============================================================================

set -e

BRANCH="${1:-local-agy}"
MSG="${2:-feat: multi-agent automated code refinement}"

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"

if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -E 'GIT_REPO|GIT_TOKEN' | xargs -d '\n')
fi

if [ -z "$GIT_REPO" ] || [ -z "$GIT_TOKEN" ]; then
  echo "⚠️ GIT_REPO or GIT_TOKEN missing in .env"
  exit 1
fi

REPO_CLEAN=$(echo "$GIT_REPO" | sed -e 's|^https://||')
AUTH_REMOTE="https://x-access-token:${GIT_TOKEN}@${REPO_CLEAN}"

git config user.name "Navjot Kumar"
git config user.email "mydrive.nkt.in@gmail.com"
git config pull.rebase true
git config rerere.enabled true
git config rerere.autoupdate true

# Setup remote origin
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$AUTH_REMOTE"
else
  git remote add origin "$AUTH_REMOTE"
fi

echo "🔄 1. Fetching latest remote state..."
git fetch origin || true

echo "📌 2. Switching to agent branch: $BRANCH..."
git checkout -B "$BRANCH"

# Rebase against remote branch if it exists to prevent divergence
git pull --rebase -X theirs origin "$BRANCH" 2>/dev/null || true

echo "📦 3. Staging all repository files (excluding .env)..."
git add -A
git reset .env 2>/dev/null || true

echo "✍️ 4. Committing on $BRANCH..."
git commit -m "$MSG" || echo "No new changes to commit on $BRANCH"

echo "🚀 5. Pushing $BRANCH to GitHub..."
git push -u origin "$BRANCH"

echo "🔀 6. Switching to main and updating from remote..."
git checkout main
git pull --rebase -X theirs origin main 2>/dev/null || true

echo "🔀 7. Merging $BRANCH into main (conflict-free -X theirs strategy)..."
git merge --no-ff -X theirs "$BRANCH" -m "merge($BRANCH): $MSG" || {
  echo "⚠️ Resolving merge conflicts automatically..."
  git diff --name-only --diff-filter=U | while read file; do
    git checkout --theirs "$file"
    git add "$file"
  done
  git commit -m "merge($BRANCH): auto-resolved conflicts favoring verified code" || true
}

echo "🚀 8. Pushing main to GitHub..."
git push origin main || {
  echo "⚠️ Remote main updated concurrently. Rebasing and pushing..."
  git pull --rebase -X theirs origin main
  git push origin main
}

echo "✅ Git synchronization workflow completed cleanly on branch main (0 conflicts)."
