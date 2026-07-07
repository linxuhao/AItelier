# core/git_ops.py
# GitHub pull-request creation for the repository panel. Kept out of
# workspace_manager (which owns local git) because this is the one repo action
# that talks to the GitHub REST API and needs the GITHUB_TOKEN secret.

import os
import os
import re
import subprocess
import os
from pathlib import Path

import httpx

from core.ai_router import _read_secret

# Force English locale for all git subprocess calls — prevents French
# locale leakage in dashboard "Make PR" action result messages.
_GIT_ENV = {"LC_ALL": "C", **os.environ}


def parse_github_owner_repo(remote_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub remote URL.

    Handles https (with or without an embedded token / .git suffix) and SSH
    (git@github.com:owner/repo.git) forms. Raises ValueError otherwise.
    """
    url = remote_url.strip()
    # SSH: git@github.com:owner/repo(.git)
    m = re.match(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    # HTTPS: https://[user[:token]@]github.com/owner/repo(.git)
    m = re.match(
        r"^https?://(?:[^@/]+@)?github\.com/([^/]+)/(.+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Not a recognised GitHub remote URL: {remote_url}")


def create_github_pr(code_path: Path, title: str, body: str = "",
                     base: str = "main", head: str | None = None) -> dict:
    """Open a PR on the origin GitHub repo via the REST API.

    head defaults to the repo's current branch. Requires a GITHUB_TOKEN secret
    with pull_requests:write. Raises RuntimeError with a useful message on any
    failure (no token, bad remote, GitHub error).
    """
    token = _read_secret("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "No GITHUB_TOKEN configured — add a fine-grained PAT at "
            "~/.aitelier-secrets/GITHUB_TOKEN (Pull requests: R/W).")

    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=code_path, capture_output=True, text=True, env=_GIT_ENV,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        raise RuntimeError("No 'origin' remote configured")
    owner, repo = parse_github_owner_repo(remote.stdout.strip())

    if not head:
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=code_path, capture_output=True, text=True, env=_GIT_ENV,
        ).stdout.strip()
    if head == base:
        raise RuntimeError(
            f"Head and base are the same branch ('{base}') — push a feature "
            "branch first.")

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "head": head, "base": base},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"GitHub request failed: {e}") from e

    if resp.status_code >= 300:
        detail = ""
        try:
            data = resp.json()
            detail = data.get("message", "")
            errs = data.get("errors") or []
            if errs:
                detail += " — " + "; ".join(
                    str(x.get("message") or x) for x in errs)
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"GitHub PR creation failed ({resp.status_code}): {detail}")

    pr = resp.json()
    return {"number": pr.get("number"), "url": pr.get("html_url"),
            "head": head, "base": base}
