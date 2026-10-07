# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os
import subprocess

from container.lib.git_askpass import _create_askpass_script
from container.lib.git_safety import ALLOWED_GIT_PROTOCOLS, authenticated_remote_url

# Re-exported so existing tests can patch ``container.tools.git_clone.
# _create_askpass_script`` directly.
__all__ = ["git_clone", "_create_askpass_script"]


def _clone_error(
    exc: subprocess.CalledProcessError, repo_url: str, base_branch: str
) -> RuntimeError:
    """Translate a failed ``git clone`` into a user-facing RuntimeError.

    The stderr tail never contains the token: GIT_ASKPASS supplies it out
    of band and the clone URL only carries the ``x-access-token`` username.
    """
    stderr = exc.stderr or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if "Remote branch" in stderr and "not found" in stderr:
        return RuntimeError(
            f"base_branch {base_branch!r} not found on remote {repo_url}"
        )
    if (
        "Repository not found" in stderr
        or "could not read Username" in stderr
        or "Authentication failed" in stderr
    ):
        return RuntimeError(
            f"repository {repo_url} not found or access denied for the "
            "connected git account"
        )
    return RuntimeError(
        f"git clone failed (exit {exc.returncode}): {stderr.strip()[-300:]}"
    )


def git_clone(
    repo_url: str,
    token: str,
    base_branch: str,
    work_dir: str,
) -> None:
    """Shallow-clone ``base_branch`` of ``repo_url`` into ``work_dir``."""
    # Build clone URL with username only — no token in the URL
    clone_url = authenticated_remote_url(repo_url)

    askpass_path = _create_askpass_script(token)
    try:
        # GIT_ALLOW_PROTOCOL keeps the clone (and any redirect it follows)
        # on HTTPS.
        env = {
            **os.environ,
            "GIT_ASKPASS": askpass_path,
            "GIT_ALLOW_PROTOCOL": ALLOWED_GIT_PROTOCOLS,
        }
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "-b", base_branch, clone_url, work_dir],
                check=True, capture_output=True, env=env,
            )
        except subprocess.CalledProcessError as exc:
            raise _clone_error(exc, repo_url, base_branch) from exc
    finally:
        if os.path.exists(askpass_path + ".token"):
            os.remove(askpass_path + ".token")
        if os.path.exists(askpass_path):
            os.remove(askpass_path)
