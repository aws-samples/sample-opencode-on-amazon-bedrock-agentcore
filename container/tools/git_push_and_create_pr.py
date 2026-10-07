# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from typing import Optional, TypedDict

from container.lib.git_askpass import _create_askpass_script
from container.lib.git_safety import (
    GIT_HARDEN_ARGS,
    authenticated_remote_url,
    hardened_git_env,
)

# Re-exported so tests can patch ``container.tools.git_push_and_create_pr.
# _create_askpass_script`` the same way they patch it for git_clone.
__all__ = ["git_push_and_create_pr", "_create_askpass_script"]

logger = logging.getLogger(__name__)

# Hardening flags applied to EVERY git invocation so a planted repo hook,
# fsmonitor command or credential helper cannot run during the push (see
# ``container.lib.git_safety.GIT_HARDEN_ARGS``). The env from
# ``hardened_git_env`` additionally disables the system/global config files
# and restricts transports to HTTPS. Repository-local config is handled by
# the pipeline, which restores ``.git/config`` from its post-clone snapshot
# before this function runs.
_HARDEN = list(GIT_HARDEN_ARGS)

# A full commit id (SHA-1 or SHA-256 object format).
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class PushResult(TypedDict):
    pr_url: Optional[str]
    pushed: bool


def git_push_and_create_pr(
    work_dir: str,
    token: str,
    repo_url: str,
    target_branch: str,
    base_branch: str,
    task_description: str,
    job_id: str,
    base_sha: str | None = None,
) -> PushResult:
    """Push branch and create GitHub pull request.

    ``base_sha`` is the commit the branch was created from. When given,
    any commits OpenCode made itself are folded back into the index
    (``git reset --soft base_sha``) before staging, so the pushed branch is
    exactly one commit on top of ``base_sha`` whose content is the scanned
    working tree. A secret the agent committed and later removed is
    therefore not reachable from the pushed ref. If the index then matches
    ``base_sha`` (no net change, including "agent committed then
    reverted"), nothing is pushed and no PR is opened.

    Pushes with a 3-retry rebase loop and creates a PR via the GitHub API.
    The push URL is built from the validated ``repo_url`` (not
    ``.git/config``) so a rewritten ``remote.origin.url`` is ignored. All
    git commands run with ``_HARDEN`` and ``hardened_git_env``;
    network-touching commands additionally set ``GIT_ASKPASS`` to a
    short-lived script that echoes the caller's token.
    """
    # Validate inputs before running any git command.
    push_url = authenticated_remote_url(repo_url)
    if base_sha is not None and not _SHA_RE.fullmatch(base_sha):
        raise ValueError(f"base_sha is not a full commit id: {base_sha!r}")

    base_env = hardened_git_env()

    if base_sha is not None:
        subprocess.run(
            ["git", *_HARDEN, "reset", "--soft", base_sha],
            cwd=work_dir, check=True, capture_output=True, env=base_env,
        )

    subprocess.run(
        ["git", *_HARDEN, "add", "-A"],
        cwd=work_dir, check=True, capture_output=True, env=base_env,
    )

    # After the soft reset HEAD is base_sha, so this compares the staged
    # tree with the base. ``check=True`` fails the push on a broken command
    # rather than treating it as "no changes".
    diff = subprocess.run(
        ["git", *_HARDEN, "diff", "--cached", "--stat"],
        cwd=work_dir, capture_output=True, text=True, check=True, env=base_env,
    )
    if not diff.stdout.strip():
        return {"pr_url": None, "pushed": False}

    subprocess.run(
        ["git", *_HARDEN, "commit", "-m", f"opencode: {job_id}"],
        cwd=work_dir, check=True, capture_output=True, env=base_env,
    )

    # Push with 3-retry rebase logic — ALL remote ops use the askpass
    # script so push / fetch can authenticate.
    askpass_path = _create_askpass_script(token)
    try:
        git_env = {**base_env, "GIT_ASKPASS": askpass_path}
        MAX_PUSH_RETRIES = 3
        for attempt in range(1, MAX_PUSH_RETRIES + 1):
            try:
                subprocess.run(
                    ["git", *_HARDEN, "push", push_url,
                     f"HEAD:refs/heads/{target_branch}"],
                    cwd=work_dir, check=True, capture_output=True, env=git_env,
                )
                break  # Push succeeded
            except subprocess.CalledProcessError as push_err:
                if attempt == MAX_PUSH_RETRIES:
                    # Surface the underlying git stderr so callers see why
                    # the push actually failed, not just exit code 128.
                    stderr = (push_err.stderr or b"").decode("utf-8", errors="replace")
                    logger.error(
                        "git push failed on attempt %d/%d: %s",
                        attempt, MAX_PUSH_RETRIES, stderr[:500],
                    )
                    raise
                # Rebase on latest remote before retrying. Fetch from the
                # explicit push URL (not the origin remote name).
                subprocess.run(
                    ["git", *_HARDEN, "fetch", push_url, base_branch],
                    cwd=work_dir, check=True, capture_output=True, env=git_env,
                )
                subprocess.run(
                    ["git", *_HARDEN, "rebase", "FETCH_HEAD"],
                    cwd=work_dir, check=True, capture_output=True, env=git_env,
                )
    finally:
        if os.path.exists(askpass_path + ".token"):
            os.remove(askpass_path + ".token")
        if os.path.exists(askpass_path):
            os.remove(askpass_path)

    # Create PR via GitHub API
    match = re.search(r"github\.com/([^/]+)/([^/.]+)", repo_url)
    if not match:
        return {"pr_url": None, "pushed": True}

    owner, repo = match.group(1), match.group(2)
    pr_body = json.dumps({
        "title": task_description[:200],
        "body": f"Job: {job_id}\n\nGenerated by OpenCode on AgentCore.",
        "head": target_branch,
        "base": base_branch,
        "labels": ["opencode-generated"],
    })

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            data=pr_body.encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        pr = json.loads(resp.read().decode())
        return {"pr_url": pr.get("html_url"), "pushed": True}
    except urllib.error.HTTPError as exc:
        body_snippet = ""
        try:
            body_snippet = exc.read().decode()[:200]
        except Exception:
            pass
        logger.warning(
            "GitHub API HTTP error %d creating PR for %s/%s: %s",
            exc.code, owner, repo, body_snippet,
        )
        return {"pr_url": None, "pushed": True}
    except urllib.error.URLError as exc:
        logger.warning(
            "GitHub API URL error creating PR for %s/%s: %s",
            owner, repo, exc.reason,
        )
        return {"pr_url": None, "pushed": True}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Failed to parse GitHub API response for %s/%s: %s",
            owner, repo, exc,
        )
        return {"pr_url": None, "pushed": True}
