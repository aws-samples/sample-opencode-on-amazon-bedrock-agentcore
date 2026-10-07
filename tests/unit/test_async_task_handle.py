# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the AgentCore async-task handle is kept and completed.

``BedrockAgentCoreApp.add_async_task(name)`` returns an integer handle and
``complete_async_task(handle)`` must receive that exact value; passing the
job_id string is rejected by the SDK with a warning log ("Attempted to
complete unknown task ID") and leaves the Runtime reporting HealthyBusy forever.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from container.code_mcp_server import (
    _cancel_flags,
    _running_tasks,
    run_coding_task,
)


async def _drain(job_id: str) -> None:
    task = _running_tasks.get(job_id)
    if task is None:
        return
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_complete_async_task_receives_add_async_task_handle(caplog):
    """complete_async_task gets the int handle, not the job_id string."""
    mock_app = MagicMock()
    mock_app.add_async_task.return_value = 7245137115325630182
    mock_app.complete_async_task.return_value = True
    mock_pipeline = AsyncMock(return_value={"status": "complete", "duration_seconds": 1.0})

    with (
        patch("container.code_mcp_server.app", mock_app),
        patch("container.code_mcp_server.run_coding_pipeline", mock_pipeline),
        caplog.at_level(logging.INFO, logger="container.code_mcp_server"),
    ):
        result = await run_coding_task(
            task_description="task",
            repo_url="https://github.com/o/r",
            base_branch="main",
            _user_id="user-1",
        )
        job_id = result["job_id"]
        await _drain(job_id)

    mock_app.add_async_task.assert_called_once_with(job_id)
    mock_app.complete_async_task.assert_called_once_with(7245137115325630182)
    (handle_arg,) = mock_app.complete_async_task.call_args.args
    assert isinstance(handle_arg, int)
    assert handle_arg != job_id

    # The outcome is logged so a False (unknown handle) is visible in CloudWatch.
    assert any(
        "complete_async_task(7245137115325630182)" in msg and job_id in msg and "True" in msg
        for msg in caplog.messages
    ), caplog.messages

    assert job_id not in _running_tasks
    assert job_id not in _cancel_flags


@pytest.mark.asyncio
async def test_complete_async_task_handle_used_even_when_pipeline_raises():
    """The finally block still completes the correct handle on pipeline failure."""
    mock_app = MagicMock()
    mock_app.add_async_task.return_value = 42
    mock_pipeline = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch("container.code_mcp_server.app", mock_app),
        patch("container.code_mcp_server.run_coding_pipeline", mock_pipeline),
    ):
        result = await run_coding_task(
            task_description="task",
            repo_url="https://github.com/o/r",
            base_branch="main",
            _user_id="user-1",
        )
        await _drain(result["job_id"])

    mock_app.complete_async_task.assert_called_once_with(42)
