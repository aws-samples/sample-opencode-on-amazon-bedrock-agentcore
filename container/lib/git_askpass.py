# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helper for ``GIT_ASKPASS``-based HTTPS token auth.

Shared between :mod:`container.tools.git_clone` and
:mod:`container.tools.git_push_and_create_pr` so every git subprocess
that needs to talk to the remote uses the same short-lived, owner-only
script.
"""

import os
import stat
import tempfile


def _create_askpass_script(token: str) -> str:
    """Create a temporary shell script that prints ``token`` for GIT_ASKPASS.

    The token is written to a sidecar file (``<script>.token``) with
    owner-read-only permissions (0o400) and the script reads it via
    ``cat "$0.token"``, so no token bytes ever appear in the shell
    source.  The trailing ``echo`` preserves the single-newline-after-
    token contract that ``GIT_ASKPASS`` callers expect.

    The script itself is owner-readable and owner-executable (0o500).
    Callers MUST remove **both** the script and the sidecar when done
    (typically from a ``finally`` block).
    """
    fd, path = tempfile.mkstemp(prefix="git_askpass_", suffix=".sh")

    # Write token bytes to a sidecar file readable only by the owner.
    sidecar = path + ".token"
    sidecar_fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    with os.fdopen(sidecar_fd, "w") as sf:
        sf.write(token)

    # Write the shell script that reads the sidecar via cat.
    with os.fdopen(fd, "w") as f:
        f.write('#!/bin/sh\ncat "$0.token"\necho\n')

    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
    return path
