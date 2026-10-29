#!/usr/bin/env bash
# ==============================================================================
# Multi-Agent Git Synchronizer (Gemini Quant Arena)
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

# Setup remote origin
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$AUTH_REMOTE"
else
  git remote add origin "$AUTH_REMOTE"
fi

echo "📌 1. Switching to agent branch: $BRANCH..."
git checkout -B "$BRANCH"

echo "📦 2. Staging all files (excluding .env)..."
git add -A
git reset .env 2>/dev/null || true

echo "✍️ 3. Committing..."
git commit -m "$MSG" || echo "No changes to commit on $BRANCH"

echo "🚀 4. Pushing $BRANCH to GitHub..."
git push -u origin "$BRANCH" || echo "Push to $BRANCH failed. Please check GitHub token write permissions."

echo "🔀 5. Merging $BRANCH into main..."
git checkout main
git merge "$BRANCH" -m "merge($BRANCH): $MSG"

echo "🚀 6. Pushing main to GitHub..."
git push origin main || echo "Push to main failed. Please check GitHub token write permissions."

echo "✅ Git synchronization workflow completed on branch main."
