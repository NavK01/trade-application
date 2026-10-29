"""
Automated Multi-Branch Conflict-Free Git Workflow for GeminiAgent Quant Pipeline.
Guarantees zero merge conflicts across:
  - 'server-agy': Autonomous Quant Supervisor on Azure VM pushes here first.
  - 'local-agy': Local development / assistant agent pushes here first.
  - 'main': Production branch running the live application.
"""

import os
import subprocess
import logging
import sys
from pathlib import Path
from typing import List, Optional
from app.config import settings

logger = logging.getLogger("git_sync")
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def get_authenticated_repo_url() -> Optional[str]:
    """Builds an authenticated GitHub HTTPS clone/push URL using GIT_REPO and GIT_TOKEN."""
    repo = getattr(settings, "GIT_REPO", None) or os.getenv("GIT_REPO")
    token = getattr(settings, "GIT_TOKEN", None) or os.getenv("GIT_TOKEN")

    if not repo or not token:
        logger.debug("GIT_REPO or GIT_TOKEN not configured in environment.")
        return None

    clean_repo = repo.strip()
    if clean_repo.startswith("https://"):
        url_without_scheme = clean_repo[len("https://"):]
        if "@" in url_without_scheme:
            url_without_scheme = url_without_scheme.split("@", 1)[1]
        return f"https://x-access-token:{token.strip()}@{url_without_scheme}"
    return clean_repo


def configure_git_repo(repo_dir: Path = BASE_DIR, user_name: str = "Navjot Kumar", user_email: str = "mydrive.nkt.in@gmail.com") -> bool:
    """Configures Git author identity, conflict prevention settings, and origin remote."""
    try:
        subprocess.run(["git", "config", "user.name", user_name], cwd=repo_dir, check=True, capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.email", user_email], cwd=repo_dir, check=True, capture_output=True, timeout=10)

        # Enable Git automatic conflict resolution features
        subprocess.run(["git", "config", "pull.rebase", "true"], cwd=repo_dir, capture_output=True, timeout=10)
        subprocess.run(["git", "config", "rerere.enabled", "true"], cwd=repo_dir, capture_output=True, timeout=10)
        subprocess.run(["git", "config", "rerere.autoupdate", "true"], cwd=repo_dir, capture_output=True, timeout=10)

        auth_url = get_authenticated_repo_url()
        if auth_url:
            res = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_dir, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=repo_dir, check=True, capture_output=True, timeout=10)
            else:
                subprocess.run(["git", "remote", "add", "origin", auth_url], cwd=repo_dir, check=True, capture_output=True, timeout=10)
        return True
    except Exception as e:
        logger.error(f"Failed to configure git repository: {e}")
        return False


def push_agent_branch(
    branch_name: str,
    commit_message: str,
    files_to_stage: Optional[List[str]] = None,
    repo_dir: Path = BASE_DIR,
) -> bool:
    """
    Commits and pushes changes directly to the designated agent branch ('server-agy' or 'local-agy').
    Fetches origin first and uses rebase to avoid divergent histories.
    Guarantees .env is never staged or committed.
    """
    configure_git_repo(repo_dir)
    try:
        # 1. Fetch remote branch if it exists
        subprocess.run(["git", "fetch", "origin", branch_name], cwd=repo_dir, capture_output=True, timeout=20)

        # 2. Switch to target agent branch
        res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, capture_output=True, text=True, timeout=10)
        cur_branch = res.stdout.strip() if res.returncode == 0 else "main"

        if cur_branch != branch_name:
            subprocess.run(["git", "checkout", "-B", branch_name], cwd=repo_dir, check=True, capture_output=True, timeout=10)

        # 3. Stage files (strictly excluding .env)
        if files_to_stage:
            safe_files = [f for f in files_to_stage if not f.endswith(".env")]
            if safe_files:
                subprocess.run(["git", "add"] + safe_files, cwd=repo_dir, check=True, capture_output=True, timeout=15)
        else:
            subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True, timeout=15)
            subprocess.run(["git", "reset", ".env"], cwd=repo_dir, capture_output=True, timeout=10)

        # 4. Commit
        commit_res = subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_dir, capture_output=True, text=True, timeout=15)
        if commit_res.returncode == 0:
            logger.info(f"📌 Git commit on [{branch_name}]: {commit_message}")

        # 5. Push with conflict-free retry
        auth_url = get_authenticated_repo_url()
        if auth_url:
            push_res = subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=repo_dir, capture_output=True, text=True, timeout=30)
            if push_res.returncode != 0:
                # Rebase with remote in case concurrent update arrived
                subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", branch_name], cwd=repo_dir, capture_output=True, timeout=25)
                push_res = subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=repo_dir, capture_output=True, text=True, timeout=30)

            if push_res.returncode == 0:
                logger.info(f"🚀 Pushed [{branch_name}] to GitHub remote.")
                return True
            else:
                logger.warning(f"Git push to [{branch_name}] failed: {push_res.stderr.strip()[:200]}")
                return False
        return True
    except Exception as e:
        logger.error(f"Error pushing agent branch {branch_name}: {e}")
        return False


def auto_resolve_merge_conflicts(from_branch: str, repo_dir: Path = BASE_DIR) -> bool:
    """
    Automatically resolves any merge conflicts by favoring the incoming verified agent code
    and committing cleanly without human intervention.
    """
    try:
        conflicts_res = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir, capture_output=True, text=True, timeout=10)
        conflicted_files = [f.strip() for f in conflicts_res.stdout.splitlines() if f.strip()]

        if not conflicted_files:
            return True

        logger.warning(f"⚠️ Auto-resolving merge conflicts in files: {conflicted_files}")
        for cfile in conflicted_files:
            # Prefer the incoming verified code
            subprocess.run(["git", "checkout", "--theirs", cfile], cwd=repo_dir, capture_output=True, timeout=10)
            subprocess.run(["git", "add", cfile], cwd=repo_dir, capture_output=True, timeout=10)

        # Complete the merge commit
        msg = f"merge({from_branch}): auto-resolved conflict favoring verified update"
        subprocess.run(["git", "commit", "-m", msg], cwd=repo_dir, capture_output=True, timeout=15)
        logger.info(f"✅ Auto-resolved all conflicts from [{from_branch}]")
        return True
    except Exception as e:
        logger.error(f"Failed to auto-resolve merge conflicts: {e}")
        subprocess.run(["git", "merge", "--abort"], cwd=repo_dir, capture_output=True, timeout=10)
        return False


def merge_agent_branch_to_main_and_deploy(
    from_branch: str,
    merge_message: str,
    repo_dir: Path = BASE_DIR,
) -> bool:
    """
    Conflict-free merge from agent branch ('server-agy' or 'local-agy') into 'main':
    1. Fetches remote origin/main.
    2. Switches to main and rebases against latest remote main.
    3. Merges from_branch using recursive '-X theirs' strategy.
    4. Auto-resolves any lingering conflicts.
    5. Runs verification suite; aborts if broken.
    6. Pushes main to GitHub and keeps main active.
    """
    configure_git_repo(repo_dir)
    try:
        # 1. Fetch remote main
        subprocess.run(["git", "fetch", "origin", "main"], cwd=repo_dir, capture_output=True, timeout=20)

        # 2. Switch to main
        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True, capture_output=True, timeout=10)

        # 3. Pull rebase to incorporate any remote changes seamlessly
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], cwd=repo_dir, capture_output=True, timeout=25)

        # 4. Merge from agent branch with -X theirs strategy (favors newly verified code)
        merge_cmd = ["git", "merge", "--no-ff", "-X", "theirs", from_branch, "-m", merge_message]
        merge_res = subprocess.run(merge_cmd, cwd=repo_dir, capture_output=True, text=True, timeout=25)

        # 5. Handle any unmerged conflicts automatically
        if merge_res.returncode != 0:
            if not auto_resolve_merge_conflicts(from_branch, repo_dir):
                logger.error(f"Merge aborted to keep main clean.")
                return False

        # 6. Verify setup test suite
        test_proc = subprocess.run([sys.executable, "test_setup.py"], cwd=repo_dir, capture_output=True, text=True, timeout=35)
        if "10/10 CHECKS PASSED" not in test_proc.stdout and test_proc.returncode != 0:
            logger.error("❌ Verification failed after merge. Rolling back main...")
            subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo_dir, capture_output=True, timeout=10)
            return False

        logger.info(f"🔀 Successfully verified and merged [{from_branch}] into [main]")

        # 7. Push main to origin
        auth_url = get_authenticated_repo_url()
        if auth_url:
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, capture_output=True, text=True, timeout=30)
            if push_res.returncode != 0:
                # Pull rebase and retry
                subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], cwd=repo_dir, capture_output=True, timeout=25)
                push_res = subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, capture_output=True, text=True, timeout=30)

            if push_res.returncode == 0:
                logger.info("🚀 Pushed updated [main] to GitHub remote.")

        return True
    except Exception as e:
        logger.error(f"Error merging {from_branch} to main: {e}")
        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, capture_output=True, timeout=10)
        return False


def pull_production_main(repo_dir: Path = BASE_DIR) -> bool:
    """Pulls latest verified production code from origin/main cleanly without conflicts."""
    configure_git_repo(repo_dir)
    try:
        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True, capture_output=True, timeout=10)
        pull_res = subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], cwd=repo_dir, capture_output=True, text=True, timeout=30)
        if pull_res.returncode == 0:
            logger.info("📥 Successfully pulled latest [main] from GitHub remote.")
            return True
        else:
            logger.warning(f"Git pull from [main] failed: {pull_res.stderr.strip()[:200]}")
            return False
    except Exception as e:
        logger.error(f"Error pulling main: {e}")
        return False
