# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the ``_elicit_with_timeout`` helper and the ``code`` tool's
handling of elicitation failures.

- The helper returns None when ``ctx.elicit()`` blocks beyond
  ``ELICITATION_TIMEOUT_S`` or raises any non-timeout exception (logged at
  WARNING with exc_info).
- The ``code`` tool returns ``GIT_HOST_NOT_CONNECTED_MESSAGE`` rather than
  the raw exception text when ``ctx.elicit`` raises.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import container.pipeline as pipeline_module
from container.code_mcp_server import _elicit_with_timeout, code
from container.lib.credential_errors import GIT_HOST_NOT_CONNECTED_MESSAGE

_AUTH_REQUIRED_CRED: dict = {
    "authorization_required": True,
    "auth_url": "https://github.com/login/device/abc",
}


def _make_ctx(elicit_side_effect: BaseException | None = None) -> MagicMock:
    """FastMCP-like Context whose awaitable ``elicit`` raises on demand."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=elicit_side_effect)
    ctx.report_progress = AsyncMock(return_value=None)
    ctx.request = None
    return ctx


@pytest.mark.asyncio
async def test_elicit_with_timeout_returns_none_on_timeout():
    """Mock ctx.elicit to block indefinitely; assert helper returns None."""
    never_done = asyncio.Event()

    ctx = MagicMock()
    ctx.elicit = MagicMock(return_value=never_done.wait())

    with patch("container.code_mcp_server.ELICITATION_TIMEOUT_S", 0.1):
        result = await _elicit_with_timeout(
            ctx,
            message="test prompt",
            schema={"type": "object", "properties": {}},
        )

    assert result is None


@pytest.mark.asyncio
async def test_elicit_with_timeout_returns_result_on_success():
    """When ctx.elicit resolves normally, the helper returns its result."""
    expected = MagicMock(action="submit", data={"confirmation": "done"})
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=expected)

    with patch("container.code_mcp_server.ELICITATION_TIMEOUT_S", 5):
        result = await _elicit_with_timeout(
            ctx,
            message="test prompt",
            schema={"type": "object", "properties": {}},
        )

    assert result is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        TypeError("Context.elicit() got an unexpected keyword argument 'schema'"),
        AttributeError("'Context' object has no attribute 'elicit'"),
        ConnectionError("gateway closed"),
        RuntimeError("elicitation backend exploded"),
    ],
    ids=["type_error", "attribute_error", "connection_error", "runtime_error"],
)
async def test_elicit_with_timeout_non_timeout_exception_returns_none_and_logs(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _make_ctx(elicit_side_effect=exc)

    with caplog.at_level(logging.WARNING, logger="container.code_mcp_server"):
        result = await _elicit_with_timeout(
            ctx,
            message="test prompt",
            schema={"type": "object", "properties": {}},
        )

    assert result is None
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records
    assert any(r.exc_info for r in warning_records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        TypeError("Context.elicit() got an unexpected keyword argument 'schema'"),
        AttributeError("'Context' object has no attribute 'elicit'"),
        ConnectionError("gateway closed"),
    ],
    ids=["type_error", "attribute_error", "connection_error"],
)
async def test_code_tool_elicit_exception_returns_user_friendly_error(
    exc: BaseException,
) -> None:
    """When OAuth is needed and ``ctx.elicit`` raises, the ``code`` tool
    reports ``GIT_HOST_NOT_CONNECTED_MESSAGE`` instead of the raw text."""
    ctx = _make_ctx(elicit_side_effect=exc)

    with (
        patch.object(
            pipeline_module,
            "resolve_git_credential",
            return_value=dict(_AUTH_REQUIRED_CRED),
        ),
        patch.object(
            pipeline_module, "write_job_record", new=AsyncMock(return_value=None)
        ),
        patch.object(
            pipeline_module, "update_job_status", new=AsyncMock(return_value=None)
        ),
    ):
        result = await code(
            task_description="add a README",
            repo_url="https://github.com/owner/repo",
            base_branch="main",
            _user_id="user-1",
            ctx=ctx,
        )

    assert result["status"] == "failed", result
    assert result["error"] == GIT_HOST_NOT_CONNECTED_MESSAGE, result
