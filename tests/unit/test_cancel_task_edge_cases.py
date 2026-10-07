# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the ``cancel_task`` response contract.

``cancel_task`` only reports (and records) ``CANCELLED`` when the job was
actually stopped or is provably no longer running:

- in-process: the asyncio task finished after ``task.cancel()`` and the
  pipeline recorded CANCELLED itself          -> ``method == "in_process"``
- cross-session: ``StopRuntimeSession`` succeeded -> ``"stop_runtime_session"``
- cross-session: ``ResourceNotFoundException``    -> ``"session_already_terminated"``

Anything else returns ``{"error": "cancel_failed", "status": <current>,
"detail": ...}`` and writes NOTHING to DynamoDB.

Validates: Requirements 6.1, 6.2, 6.3
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import container.code_mcp_server as server
from container.code_mcp_server import (
    _cancel_flags,
    _running_tasks,
    cancel_task,
)
from container.lib.dynamodb_helpers import JobStateConflict

_ARN = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/test-rt"
_SESSION = "f67ddcd1-dc44-4867-8793-e4888e672b6b"


def _running_record(job_id: str, user_id: str, session_id: str = _SESSION, **extra) -> dict:
    rec = {
        "job_id": job_id,
        "status": "RUNNING",
        "user_id": user_id,
        "runtime_session_id": session_id,
        "created_at": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    }
    rec.update(extra)
    return rec


def _rnf_error(session_id: str = _SESSION) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": f"Session {session_id} not found or has been terminated",
            }
        },
        "StopRuntimeSession",
    )


class _Harness:
    """Patch query/update/boto3/_get_runtime_arn around one cancel_task call."""

    def __init__(self, record, *, stop_side_effect=None, arn=_ARN, update_side_effect=None,
                 records=None):
        self.records = list(records) if records is not None else [record]
        self.query_calls = 0
        self.update_calls: list[dict] = []
        self.stop_calls: list[dict] = []
        self._stop_side_effect = stop_side_effect
        self._arn = arn
        self._update_side_effect = update_side_effect

    async def _query(self, job_id, user_id):
        idx = min(self.query_calls, len(self.records) - 1)
        self.query_calls += 1
        rec = self.records[idx]
        return dict(rec) if rec is not None else None

    async def _update(self, job_id, user_id, status, **kwargs):
        self.update_calls.append({"job_id": job_id, "user_id": user_id, "status": status, **kwargs})
        if self._update_side_effect is not None:
            raise self._update_side_effect

    def _stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        if self._stop_side_effect is not None:
            raise self._stop_side_effect
        return {"runtimeSessionId": kwargs.get("runtimeSessionId"), "statusCode": 200}

    def patches(self):
        client = MagicMock()
        client.stop_runtime_session = self._stop
        return (
            patch("container.code_mcp_server.query_job_record", side_effect=self._query),
            patch("container.code_mcp_server.update_job_status", side_effect=self._update),
            patch("container.code_mcp_server._get_runtime_arn", return_value=self._arn),
            patch("container.code_mcp_server.boto3.client", return_value=client),
        )


async def _run(harness: _Harness, job_id: str, user_id: str) -> dict:
    p1, p2, p3, p4 = harness.patches()
    with p1, p2, p3, p4:
        return await cancel_task(job_id=job_id, _user_id=user_id)


# ---------------------------------------------------------------------------
# Unchanged early exits
# ---------------------------------------------------------------------------


class TestCancelTaskEarlyExits:
    @pytest.mark.asyncio
    async def test_returns_error_when_job_not_in_dynamodb(self):
        h = _Harness(None)
        result = await _run(h, "nonexistent-job", "user-1")
        assert result == {"error": "Job not found"}
        assert h.update_calls == []
        assert h.stop_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["COMPLETE", "FAILED", "CANCELLED"])
    async def test_returns_error_for_terminal_state(self, terminal_status):
        rec = _running_record("job-123", "user-1")
        rec["status"] = terminal_status
        h = _Harness(rec)
        result = await _run(h, "job-123", "user-1")
        assert result == {"error": f"Job is already in terminal state: {terminal_status}"}
        assert h.update_calls == []
        assert h.stop_calls == []

    @pytest.mark.asyncio
    async def test_no_user_id(self):
        assert await cancel_task(job_id="j", _user_id="") == {"error": "No user_id available"}


# ---------------------------------------------------------------------------
# Cross-session path
# ---------------------------------------------------------------------------


class TestCancelTaskCrossSession:
    @pytest.mark.asyncio
    async def test_stop_success_records_cancelled_with_full_row(self):
        """StopRuntimeSession 200 -> method stop_runtime_session + CANCELLED write."""
        job_id, user_id = "remote-job-1", "user-1"
        created = datetime.now(timezone.utc) - timedelta(seconds=42)
        rec = _running_record(job_id, user_id, created_at=created.isoformat())
        h = _Harness(rec)
        _running_tasks.pop(job_id, None)

        result = await _run(h, job_id, user_id)

        assert result == {"job_id": job_id, "status": "CANCELLED", "method": "stop_runtime_session"}
        assert h.stop_calls == [{"agentRuntimeArn": _ARN, "runtimeSessionId": _SESSION}]

        assert len(h.update_calls) == 1
        w = h.update_calls[0]
        assert w["job_id"] == job_id and w["user_id"] == user_id
        assert w["status"] == "CANCELLED"
        assert w["expected_status"] == "RUNNING"
        assert w["error"] == "Task cancelled by user"
        assert w["pr_url"] == ""
        assert w["stop_reason"] == ""
        assert w["files_edited"] == []
        assert 41.0 <= w["duration_seconds"] <= 50.0
        assert isinstance(w["duration_seconds"], float)
        datetime.fromisoformat(w["completed_at"])  # valid ISO timestamp

    @pytest.mark.asyncio
    async def test_resource_not_found_records_session_already_terminated(self):
        """ResourceNotFoundException -> the microVM is provably gone -> CANCELLED."""
        job_id, user_id = "remote-job-gone", "user-1"
        h = _Harness(_running_record(job_id, user_id), stop_side_effect=_rnf_error())

        result = await _run(h, job_id, user_id)

        assert result["job_id"] == job_id
        assert result["status"] == "CANCELLED"
        assert result["method"] == "session_already_terminated"
        assert _SESSION in result["detail"]
        assert "not found or has been terminated" in result["detail"]
        assert len(h.update_calls) == 1
        assert h.update_calls[0]["status"] == "CANCELLED"
        assert h.update_calls[0]["files_edited"] == []

    @pytest.mark.asyncio
    async def test_empty_runtime_session_id_is_cancel_failed_no_write(self):
        job_id, user_id = "remote-no-session", "user-1"
        h = _Harness(_running_record(job_id, user_id, session_id=""))

        result = await _run(h, job_id, user_id)

        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert result["job_id"] == job_id
        assert "no runtime_session_id" in result["detail"]
        assert h.update_calls == []
        assert h.stop_calls == []

    @pytest.mark.asyncio
    async def test_missing_runtime_session_id_key_is_cancel_failed(self):
        job_id, user_id = "remote-no-key", "user-1"
        rec = _running_record(job_id, user_id)
        del rec["runtime_session_id"]
        h = _Harness(rec)
        result = await _run(h, job_id, user_id)
        assert result["error"] == "cancel_failed"
        assert h.update_calls == []

    @pytest.mark.asyncio
    async def test_generic_client_error_is_cancel_failed_no_write(self):
        job_id, user_id = "remote-denied", "user-1"
        err = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}},
            "StopRuntimeSession",
        )
        h = _Harness(_running_record(job_id, user_id), stop_side_effect=err)

        result = await _run(h, job_id, user_id)

        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert "StopRuntimeSession failed: AccessDeniedException: not allowed" in result["detail"]
        assert h.update_calls == []
        assert len(h.stop_calls) == 1

    @pytest.mark.asyncio
    async def test_generic_exception_is_cancel_failed_no_write(self):
        job_id, user_id = "remote-boom", "user-1"
        h = _Harness(_running_record(job_id, user_id), stop_side_effect=RuntimeError("session gone"))

        result = await _run(h, job_id, user_id)

        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert "StopRuntimeSession failed: RuntimeError: session gone" in result["detail"]
        assert h.update_calls == []

    @pytest.mark.asyncio
    async def test_job_state_conflict_on_write_returns_terminal_status(self):
        """Write refused (row already COMPLETE) -> cancel_failed with that status."""
        job_id, user_id = "remote-race", "user-1"
        running = _running_record(job_id, user_id)
        completed = dict(running, status="COMPLETE", pr_url="https://github.com/o/r/pull/9")
        h = _Harness(
            running,
            records=[running, completed],
            update_side_effect=JobStateConflict("no longer RUNNING"),
        )

        result = await _run(h, job_id, user_id)

        assert result == {
            "job_id": job_id,
            "status": "COMPLETE",
            "error": "cancel_failed",
            "detail": "job reached a terminal state before the cancellation was recorded",
        }
        assert len(h.stop_calls) == 1
        assert len(h.update_calls) == 1  # attempted exactly once
        assert h.query_calls == 2  # initial read + re-read after the conflict

    @pytest.mark.asyncio
    async def test_job_state_conflict_with_cancelled_row_is_success(self):
        """Write refused because the killed VM's pipeline already recorded
        CANCELLED during shutdown -> the stop succeeded, report success."""
        job_id, user_id = "remote-race-cancelled", "user-1"
        running = _running_record(job_id, user_id)
        cancelled = dict(running, status="CANCELLED", error="Task cancelled")
        h = _Harness(
            running,
            records=[running, cancelled],
            update_side_effect=JobStateConflict("no longer RUNNING"),
        )

        result = await _run(h, job_id, user_id)

        assert result == {
            "job_id": job_id,
            "status": "CANCELLED",
            "method": "stop_runtime_session",
            "detail": "CANCELLED was recorded by the job's own cancellation handler",
        }
        assert "error" not in result
        assert len(h.stop_calls) == 1
        assert len(h.update_calls) == 1  # attempted exactly once, refused
        assert h.query_calls == 2

    @pytest.mark.asyncio
    async def test_conflict_with_cancelled_row_after_rnf_keeps_both_details(self):
        """session_already_terminated + handler-recorded CANCELLED -> both
        explanations appear in ``detail``, method is preserved."""
        job_id, user_id = "remote-gone-race", "user-1"
        running = _running_record(job_id, user_id)
        h = _Harness(
            running,
            records=[running, dict(running, status="CANCELLED")],
            stop_side_effect=_rnf_error(),
            update_side_effect=JobStateConflict("no longer RUNNING"),
        )

        result = await _run(h, job_id, user_id)

        assert result["status"] == "CANCELLED"
        assert result["method"] == "session_already_terminated"
        assert "error" not in result
        assert "not found or has been terminated" in result["detail"]
        assert result["detail"].endswith(
            "; CANCELLED was recorded by the job's own cancellation handler"
        )

    @pytest.mark.asyncio
    async def test_unparseable_created_at_gives_zero_duration(self):
        job_id, user_id = "remote-bad-created", "user-1"
        h = _Harness(_running_record(job_id, user_id, created_at="not-a-date"))
        result = await _run(h, job_id, user_id)
        assert result["status"] == "CANCELLED"
        assert h.update_calls[0]["duration_seconds"] == 0.0

    @pytest.mark.asyncio
    async def test_missing_created_at_gives_zero_duration(self):
        job_id, user_id = "remote-no-created", "user-1"
        rec = _running_record(job_id, user_id)
        del rec["created_at"]
        h = _Harness(rec)
        result = await _run(h, job_id, user_id)
        assert result["status"] == "CANCELLED"
        assert h.update_calls[0]["duration_seconds"] == 0.0


# ---------------------------------------------------------------------------
# In-process path (job running on this microVM)
# ---------------------------------------------------------------------------


async def _sleep_forever() -> None:
    await asyncio.sleep(3600)


class TestCancelTaskInProcess:
    @pytest.mark.asyncio
    async def test_in_process_success_skips_stop_runtime_session(self):
        """Task finishes after cancel and the pipeline wrote CANCELLED -> in_process."""
        job_id, user_id = "in-process-job-1", "user-1"
        running = _running_record(job_id, user_id)
        cancelled = dict(running, status="CANCELLED")
        # Second read (after the task finished) sees the pipeline's CANCELLED row.
        h = _Harness(running, records=[running, cancelled])

        task = asyncio.create_task(_sleep_forever())
        await asyncio.sleep(0)  # let the task body start before cancelling
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False
        try:
            result = await _run(h, job_id, user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)
            if not task.done():
                task.cancel()

        assert result == {"job_id": job_id, "status": "CANCELLED", "method": "in_process"}
        assert task.cancelled()
        assert h.stop_calls == []
        # The pipeline owns the CANCELLED write on this path; cancel_task
        # must not write a second time.
        assert h.update_calls == []
        assert h.query_calls == 2

    @pytest.mark.asyncio
    async def test_in_process_sets_cancel_flag(self):
        job_id, user_id = "in-process-flag", "user-1"
        running = _running_record(job_id, user_id)
        h = _Harness(running, records=[running, dict(running, status="CANCELLED")])
        seen_flag = {}

        async def _observe() -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                seen_flag["value"] = _cancel_flags.get(job_id)

        task = asyncio.create_task(_observe())
        await asyncio.sleep(0)  # let the task body start before cancelling
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False
        try:
            await _run(h, job_id, user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)

        assert seen_flag["value"] is True

    @pytest.mark.asyncio
    async def test_in_process_task_finished_as_complete_is_terminal_error(self):
        """Task ended COMPLETE before the cancel took effect -> nothing stopped."""
        job_id, user_id = "in-process-complete", "user-1"
        running = _running_record(job_id, user_id)
        h = _Harness(running, records=[running, dict(running, status="COMPLETE")])

        task = asyncio.create_task(_sleep_forever())
        await asyncio.sleep(0)  # let the task body start before cancelling
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False
        try:
            result = await _run(h, job_id, user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)

        assert result == {"error": "Job is already in terminal state: COMPLETE"}
        assert h.stop_calls == []
        assert h.update_calls == []

    @pytest.mark.asyncio
    async def test_in_process_timeout_falls_through_to_stop_runtime_session(self):
        """Task ignores cancel within the timeout -> cross-session fallback."""
        job_id, user_id = "in-process-stubborn", "user-1"
        h = _Harness(_running_record(job_id, user_id))

        async def _slow_to_stop() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.3)

        task = asyncio.create_task(_slow_to_stop())
        await asyncio.sleep(0)  # let the task body start before cancelling
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False
        try:
            with patch.object(server, "IN_PROCESS_CANCEL_TIMEOUT_S", 0.01):
                result = await _run(h, job_id, user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)
            await task

        assert result["status"] == "CANCELLED"
        assert result["method"] == "stop_runtime_session"
        assert "did not finish within" in result["detail"]
        assert len(h.stop_calls) == 1
        assert len(h.update_calls) == 1
        assert h.update_calls[0]["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_in_process_done_but_record_still_running_falls_through(self):
        """Task finished but no CANCELLED row (pipeline write failed) -> fallback."""
        job_id, user_id = "in-process-noroW", "user-1"
        running = _running_record(job_id, user_id)
        h = _Harness(running, records=[running, running])

        task = asyncio.create_task(_sleep_forever())
        await asyncio.sleep(0)  # let the task body start before cancelling
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False
        try:
            result = await _run(h, job_id, user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)

        assert result["status"] == "CANCELLED"
        assert result["method"] == "stop_runtime_session"
        assert "still RUNNING" in result["detail"]
        assert len(h.stop_calls) == 1
        assert len(h.update_calls) == 1

    @pytest.mark.asyncio
    async def test_in_process_timeout_then_empty_session_is_cancel_failed(self):
        job_id, user_id = "in-process-stubborn-nosession", "user-1"
        h = _Harness(_running_record(job_id, user_id, session_id=""))

        async def _slow_to_stop() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.3)

        task = asyncio.create_task(_slow_to_stop())
        await asyncio.sleep(0)  # let the task body start before cancelling
        _running_tasks[job_id] = task
        _cancel_flags[job_id] = False
        try:
            with patch.object(server, "IN_PROCESS_CANCEL_TIMEOUT_S", 0.01):
                result = await _run(h, job_id, user_id)
        finally:
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)
            await task

        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert "did not finish within" in result["detail"]
        assert "no runtime_session_id" in result["detail"]
        assert h.update_calls == []


# ---------------------------------------------------------------------------
# Docstring is the user-facing MCP tool description: pin the contract words.
# ---------------------------------------------------------------------------


def test_docstring_states_response_contract():
    doc = cancel_task.__doc__
    for needle in (
        '"status": "CANCELLED"',
        '"method": "in_process" | "stop_runtime_session" | "session_already_terminated"',
        '"error": "cancel_failed"',
        "NOT modified",
        '{"error": "Job not found"}',
        'Job is already in terminal state',
    ):
        assert needle in doc, needle
