# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared user-facing messages for credential-related error surfaces.

This module exists as a neutral location so both ``container/code_mcp_server.py``
and ``container/pipeline.py`` can import the same canonical string without
introducing a circular import (``code_mcp_server`` already imports from
``container.pipeline``).

See ``.kiro/specs/30-elicitation-error-handling/design.md`` for rationale.
"""

from __future__ import annotations

# Single source of truth for the "credentials missing, run connect_git_host"
# error surface. Any code path that needs to report a missing-credentials
# condition to the user MUST import this constant rather than duplicating
# the literal string.
GIT_HOST_NOT_CONNECTED_MESSAGE = (
    "GitHub credentials not connected. Run connect_git_host with "
    "git_host='github.com' first, then retry."
)
