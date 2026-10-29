"""
Automated Multi-Branch Git Workflow for GeminiAgent Quant Pipeline.
Manages isolated branch tracking:
  - 'server-agy': Autonomous Quant Supervisor on Azure VM pushes here first.
  - 'local-agy': Local development / assistant agent pushes here first.
  - 'main': Production branch running the live application.
"""

import os
import subprocess
import logging
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
        # Strip any existing user:token in the URL
        if "@" in url_without_scheme:
            url_without_scheme = url_without_scheme.split("@", 1)[1]
        return f"https://x-access-token:{token.strip()}@{url_without_scheme}"
    return clean_repo


def configure_git_repo(repo_dir: Path = BASE_DIR, user_name: str = "Navjot Kumar", user_email: str = "mydrive.nkt.in@gmail.com") -> bool:
    """Configures Git author identity and origin remote."""
    try:
        subprocess.run(["git", "config", "user.name", user_name], cwd=repo_dir, check=True, capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.email", user_email], cwd=repo_dir, check=True, capture_output=True, timeout=10)

        auth_url = get_authenticated_repo_url()
        if auth_url:
            # Check existing remote
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
    Ensures all files except .env are tracked.
    """
    configure_git_repo(repo_dir)
    try:
        # Check current branch or switch to target branch
        res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, capture_output=True, text=True, timeout=10)
        cur_branch = res.stdout.strip() if res.returncode == 0 else "main"

        if cur_branch != branch_name:
            # Create branch if it doesn't exist or switch to it
            subprocess.run(["git", "checkout", "-B", branch_name], cwd=repo_dir, check=True, capture_output=True, timeout=10)

        # Stage files (strictly excluding .env)
        if files_to_stage:
            safe_files = [f for f in files_to_stage if not f.endswith(".env")]
            if safe_files:
                subprocess.run(["git", "add"] + safe_files, cwd=repo_dir, check=True, capture_output=True, timeout=15)
        else:
            subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True, timeout=15)
            # Guarantee .env is never staged
            subprocess.run(["git", "reset", ".env"], cwd=repo_dir, capture_output=True, timeout=10)

        # Commit
        commit_res = subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_dir, capture_output=True, text=True, timeout=15)
        if commit_res.returncode == 0:
            logger.info(f"📌 Git commit on [{branch_name}]: {commit_message}")
        else:
            logger.debug(f"Git commit skipped (no changes or already committed): {commit_res.stdout.strip() or commit_res.stderr.strip()}")

        # Push to remote
        auth_url = get_authenticated_repo_url()
        if auth_url:
            push_res = subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=repo_dir, capture_output=True, text=True, timeout=30)
            if push_res.returncode == 0:
                logger.info(f"🚀 Pushed [{branch_name}] to GitHub remote: {settings.GIT_REPO}")
                return True
            else:
                logger.warning(f"Git push to [{branch_name}] failed: {push_res.stderr.strip()[:200]}")
                return False
        return True
    except Exception as e:
        logger.error(f"Error pushing agent branch {branch_name}: {e}")
        return False


def merge_agent_branch_to_main_and_deploy(
    from_branch: str,
    merge_message: str,
    repo_dir: Path = BASE_DIR,
) -> bool:
    """
    Merges verified changes from agent branch ('server-agy' or 'local-agy') into 'main',
    pushes 'main' to GitHub remote, and keeps 'main' checked out for live execution.
    """
    configure_git_repo(repo_dir)
    try:
        # Switch to main
        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True, capture_output=True, timeout=10)

        # Merge from agent branch
        merge_cmd = ["git", "merge", from_branch, "-m", merge_message]
        merge_res = subprocess.run(merge_cmd, cwd=repo_dir, capture_output=True, text=True, timeout=20)
        if merge_res.returncode == 0:
            logger.info(f"🔀 Successfully merged [{from_branch}] into [main]")
        else:
            logger.warning(f"Git merge warning for [{from_branch}] into [main]: {merge_res.stderr.strip()[:200]}")

        # Push main to origin
        auth_url = get_authenticated_repo_url()
        if auth_url:
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, capture_output=True, text=True, timeout=30)
            if push_res.returncode == 0:
                logger.info(f"🚀 Pushed [main] to GitHub remote: {settings.GIT_REPO}")
            else:
                logger.warning(f"Git push to [main] failed: {push_res.stderr.strip()[:200]}")

        return True
    except Exception as e:
        logger.error(f"Error merging {from_branch} to main: {e}")
        return False


def pull_production_main(repo_dir: Path = BASE_DIR) -> bool:
    """Pulls latest verified production code from origin/main into the live application."""
    configure_git_repo(repo_dir)
    try:
        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True, capture_output=True, timeout=10)
        pull_res = subprocess.run(["git", "pull", "origin", "main"], cwd=repo_dir, capture_output=True, text=True, timeout=30)
        if pull_res.returncode == 0:
            logger.info("📥 Successfully pulled latest [main] from GitHub remote.")
            return True
        else:
            logger.warning(f"Git pull from [main] failed: {pull_res.stderr.strip()[:200]}")
            return False
    except Exception as e:
        logger.error(f"Error pulling main: {e}")
        return False
