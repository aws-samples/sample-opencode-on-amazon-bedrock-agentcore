# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helpers for running git on a working tree after OpenCode has used it.

OpenCode runs with ``bash: allow`` inside the cloned repository, so by the
time the pipeline scans, commits and pushes, everything under ``.git/``
(config, hooks, remotes) may differ from what ``git_clone`` produced. The
goal of these helpers is push integrity: the push goes to the validated
``repo_url`` with the tree the scanner just inspected, and no repository
hooks or credential helpers run as part of it. To that end the pipeline:

* snapshots ``.git/config`` right after clone and branch setup
  (:func:`read_git_config`) and writes it back after OpenCode's process
  group has been killed, before the scan and push steps
  (:func:`restore_git_config`). Restoring the whole file discards every
  repository-local setting OpenCode added (remotes, ``url.*.insteadOf``,
  ``include.path`` directives);
* runs every later git command with :data:`GIT_HARDEN_ARGS` (no hooks, no
  fsmonitor, no credential helpers) and :func:`hardened_git_env` (no
  system or global config files, no terminal prompt, HTTPS transport
  only).

Remote operations additionally use an explicit URL built from the
validated ``repo_url`` (:func:`authenticated_remote_url`) rather than the
``origin`` remote.
"""

import os
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

# ``-c`` flags applied to every git invocation that runs after OpenCode.
# ``-c`` has the highest config precedence, so these override anything in
# the repository's own config:
#   * ``core.hooksPath=/dev/null`` disables any ``.git/hooks`` scripts.
#   * ``core.fsmonitor=`` (empty, i.e. false) disables any filesystem
#     monitor hook command.
#   * ``credential.helper=`` clears configured credential helpers so the
#     only credential source is the caller's ``GIT_ASKPASS`` script.
GIT_HARDEN_ARGS: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
    "-c", "credential.helper=",
)

# Transports git may use (``GIT_ALLOW_PROTOCOL``). HTTPS only: the pipeline
# only accepts ``https://`` repo URLs, the OAuth token is supplied over
# HTTPS via GIT_ASKPASS, and the Runtime security group allows outbound TCP
# 443 only. This also stops a redirect or submodule URL from switching to
# another transport (ssh, git, file, ext).
ALLOWED_GIT_PROTOCOLS = "https"


def hardened_git_env(base: Optional[Mapping[str, str]] = None) -> dict:
    """Return an env dict for git commands that run after OpenCode.

    ``base`` defaults to ``os.environ``. Inherited ``GIT_CONFIG_*`` entries
    (``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n``, ``GIT_CONFIG_PARAMETERS``)
    are dropped so they cannot add config, then:

    * ``GIT_CONFIG_NOSYSTEM=1`` and ``GIT_CONFIG_SYSTEM=/dev/null`` skip the
      system config file;
    * ``GIT_CONFIG_GLOBAL=/dev/null`` skips ``~/.gitconfig`` (which
      OpenCode could have written);
    * ``GIT_TERMINAL_PROMPT=0`` makes a missing credential fail instead of
      prompting;
    * ``GIT_ALLOW_PROTOCOL`` restricts transports to
      :data:`ALLOWED_GIT_PROTOCOLS`.
    """
    env = {
        k: v
        for k, v in (os.environ if base is None else base).items()
        if not k.startswith("GIT_CONFIG_")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = ALLOWED_GIT_PROTOCOLS
    return env


def read_git_config(work_dir: str) -> bytes:
    """Return the bytes of ``<work_dir>/.git/config``.

    Call right after clone and branch setup, before OpenCode runs.
    """
    return (Path(work_dir) / ".git" / "config").read_bytes()


def restore_git_config(work_dir: str, content: bytes) -> None:
    """Write ``content`` back to ``<work_dir>/.git/config``.

    Call after OpenCode has finished so the scan and push steps run with
    the repository config the clone produced.
    """
    (Path(work_dir) / ".git" / "config").write_bytes(content)


def authenticated_remote_url(repo_url: str) -> str:
    """Build the HTTPS URL used for clone/push/fetch from ``repo_url``.

    Uses the ``x-access-token@`` username form (the token itself comes
    from ``GIT_ASKPASS``). Only plain ``https://`` URLs with a host, a
    repository path and no embedded credentials are accepted.
    """
    parts = urlsplit(repo_url)
    if not repo_url.startswith("https://") or not parts.hostname:
        raise ValueError("repo_url must be an https:// URL with a host")
    if "@" in parts.netloc:
        raise ValueError("repo_url must not contain credentials")
    if not parts.path.strip("/"):
        raise ValueError("repo_url has no repository path")
    return repo_url.replace("https://", "https://x-access-token@", 1)
