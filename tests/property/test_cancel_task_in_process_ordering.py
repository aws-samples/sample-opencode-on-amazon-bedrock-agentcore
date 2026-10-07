# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: in-process cancellation is attempted before cross-session fallback.

Feature: 13-runtime-consolidation
Property 2: In-process cancellation is attempted before cross-session fallback

For any job_id that exists in the in-process ``_running_tasks`` registry,
``cancel_task`` SHALL cancel the asyncio task and wait for it to finish
before considering ``StopRuntimeSession``. If the task finishes (and the
pipeline recorded CANCELLED), StopRuntimeSession is NOT called and the
result is ``method == "in_process"``. If the job_id is NOT in
``_running_tasks``, ``cancel_task`` proceeds directly to StopRuntimeSession.
If the task does not finish within ``IN_PROCESS_CANCEL_TIMEOUT_S``,
StopRuntimeSession is called as the fallback, after ``task.cancel()``.

Validates: Requirements 6.1, 6.2
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import container.code_mcp_server as server
from container.code_mcp_server import (
    cancel_task,
    _running_tasks,
    _cancel_flags,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
job_id_st = st.uuids().map(str)
user_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=40,
)
session_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=50,
)

_ARN = "arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-test"


def _patches(query, update, stop, call_order):
    mock_boto_client = MagicMock()

    def _stop(**kwargs):
        call_order.append("StopRuntimeSession")
        return stop(**kwargs)

    mock_boto_client.stop_runtime_session = _stop
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_boto_client
    return (
        patch("container.code_mcp_server.query_job_record", side_effect=query),
        patch("container.code_mcp_server.update_job_status", side_effect=update),
        patch("container.code_mcp_server._get_runtime_arn", return_value=_ARN),
        patch("container.code_mcp_server.boto3", mock_boto3),
    )


class TestCancelTaskInProcessOrdering:
    """**Feature: 13-runtime-consolidation, Property 2**"""

    @given(job_id=job_id_st, user_id=user_id_st, session_id=session_id_st)
    @settings(max_examples=50, deadline=None)
    @pytest.mark.asyncio
    async def test_in_process_cancel_finishes_without_stop_session(
        self, job_id, user_id, session_id
    ):
        """**Validates: Requirements 6.1, 6.2**

        Job IS in _running_tasks and the task stops promptly: task.cancel()
        is observed, StopRuntimeSession is NOT called, method is in_process,
        and cancel_task itself writes nothing (the pipeline owns that row).
        """
        call_order: list[str] = []
        reads = [0]

        async def mock_query_job_record(job_id, user_id):
            reads[0] += 1
            # First read: RUNNING. After the task finished: the pipeline's
            # CANCELLED row.
            return {
                "job_id": job_id,
                "status": "RUNNING" if reads[0] == 1 else "CANCELLED",
                "user_id": user_id,
                "runtime_session_id": session_id,
            }

        async def mock_update_job_status(job_id, user_id, status, **kwargs):
            call_order.append(f"update:{status}")

        async def _sleeper() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                call_order.append("task.cancel")
                raise

        task = asyncio.create_task(_sleeper())
        await asyncio.sleep(0)  # let the sleeper start
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False

        p1, p2, p3, p4 = _patches(
            mock_query_job_record, mock_update_job_status, lambda **kw: None, call_order
        )
        try:
            with p1, p2, p3, p4:
                result = await cancel_task(job_id=job_id, _user_id=user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)
            if not task.done():
                task.cancel()

        assert call_order == ["task.cancel"], call_order
        assert result == {"job_id": job_id, "status": "CANCELLED", "method": "in_process"}
        assert task.cancelled()

    @given(job_id=job_id_st, user_id=user_id_st, session_id=session_id_st)
    @settings(max_examples=50, deadline=None)
    @pytest.mark.asyncio
    async def test_stop_session_called_directly_when_not_in_running_tasks(
        self, job_id, user_id, session_id
    ):
        """**Validates: Requirements 6.1, 6.2**

        Job NOT in _running_tasks: StopRuntimeSession is called directly and
        the CANCELLED row is written by cancel_task after it succeeds.
        """
        call_order: list[str] = []

        async def mock_query_job_record(job_id, user_id):
            return {
                "job_id": job_id,
                "status": "RUNNING",
                "user_id": user_id,
                "runtime_session_id": session_id,
            }

        async def mock_update_job_status(job_id, user_id, status, **kwargs):
            call_order.append(f"update:{status}")

        _running_tasks.pop(job_id, None)
        _cancel_flags.pop(job_id, None)

        p1, p2, p3, p4 = _patches(
            mock_query_job_record, mock_update_job_status, lambda **kw: None, call_order
        )
        with p1, p2, p3, p4:
            result = await cancel_task(job_id=job_id, _user_id=user_id)

        assert call_order == ["StopRuntimeSession", "update:CANCELLED"], call_order
        assert result == {
            "job_id": job_id,
            "status": "CANCELLED",
            "method": "stop_runtime_session",
        }

    @given(job_id=job_id_st, user_id=user_id_st, session_id=session_id_st)
    @settings(max_examples=25, deadline=None)
    @pytest.mark.asyncio
    async def test_fallback_to_stop_session_when_task_does_not_finish(
        self, job_id, user_id, session_id
    ):
        """**Validates: Requirements 6.1, 6.2**

        Job IS in _running_tasks but the task ignores cancellation within
        the (shortened) in-process timeout: task.cancel() is attempted FIRST,
        then StopRuntimeSession is called as the fallback, then the CANCELLED
        row is written.
        """
        call_order: list[str] = []

        async def mock_query_job_record(job_id, user_id):
            return {
                "job_id": job_id,
                "status": "RUNNING",
                "user_id": user_id,
                "runtime_session_id": session_id,
            }

        async def mock_update_job_status(job_id, user_id, status, **kwargs):
            call_order.append(f"update:{status}")

        release = asyncio.Event()

        async def _stubborn() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                call_order.append("task.cancel")
            await release.wait()

        task = asyncio.create_task(_stubborn())
        await asyncio.sleep(0)
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False

        p1, p2, p3, p4 = _patches(
            mock_query_job_record, mock_update_job_status, lambda **kw: None, call_order
        )
        try:
            with p1, p2, p3, p4, patch.object(server, "IN_PROCESS_CANCEL_TIMEOUT_S", 0.01):
                result = await cancel_task(job_id=job_id, _user_id=user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)
            release.set()
            await task

        assert call_order == ["task.cancel", "StopRuntimeSession", "update:CANCELLED"], call_order
        assert result["status"] == "CANCELLED"
        assert result["method"] == "stop_runtime_session"
        assert "did not finish within" in result["detail"]
