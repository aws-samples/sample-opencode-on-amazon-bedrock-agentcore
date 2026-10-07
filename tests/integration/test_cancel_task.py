# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: ``cancel_task`` for running async tasks.

Tests the cross-session cancellation (StopRuntimeSession) path, which is
the path every Gateway-routed cancel takes in practice (each tools/call
lands on a fresh Runtime session). The contract is honest: CANCELLED is
only returned and recorded when StopRuntimeSession succeeded or reported
the session as already terminated; any other failure returns
``cancel_failed`` and leaves DynamoDB untouched.

Requirements: 7.2, 7.5
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Stub external dependencies
# ---------------------------------------------------------------------------
fastmcp_mock = MagicMock()
fastmcp_mock.FastMCP.return_value.tool.return_value = lambda fn: fn
sys.modules.setdefault("fastmcp", fastmcp_mock)

agentcore_mock = MagicMock()
agentcore_mock.BedrockAgentCoreApp.return_value = MagicMock()
sys.modules.setdefault("bedrock_agentcore", agentcore_mock)
sys.modules.setdefault("bedrock_agentcore.runtime", agentcore_mock)

strands_mock = MagicMock()
strands_mock.tool = lambda fn: fn
sys.modules.setdefault("strands", strands_mock)

from container.code_mcp_server import (  # noqa: E402
    cancel_task,
)

_ARN = "arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-test"


class TestCancelTaskCrossSession:
    """Cross-session cancellation: task on a different microVM (Req 7.2, 7.5)."""

    @pytest.mark.asyncio
    async def test_cross_session_calls_stop_runtime_session(self):
        """StopRuntimeSession is called with the recorded session id; CANCELLED recorded."""
        job_id = "job-cross-1"

        fake_record = {
            "job_id": job_id,
            "status": "RUNNING",
            "user_id": "user-1",
            "runtime_session_id": "sess-xyz-123",
            "created_at": (
                datetime.now(timezone.utc) - timedelta(seconds=9.6)
            ).isoformat(),
        }

        mock_boto_client = MagicMock()

        with (
            patch(
                "container.code_mcp_server.query_job_record",
                new_callable=AsyncMock,
                return_value=fake_record,
            ),
            patch(
                "container.code_mcp_server.update_job_status",
                new_callable=AsyncMock,
            ) as mock_update,
            patch("container.code_mcp_server._get_runtime_arn", return_value=_ARN),
            patch("boto3.client", return_value=mock_boto_client),
        ):
            result = await cancel_task(job_id=job_id, _user_id="user-1")

        assert result["status"] == "CANCELLED"
        assert result["method"] == "stop_runtime_session"
        mock_boto_client.stop_runtime_session.assert_called_once_with(
            agentRuntimeArn=_ARN, runtimeSessionId="sess-xyz-123"
        )
        mock_update.assert_awaited_once()
        kwargs = mock_update.await_args.kwargs
        assert kwargs["status"] == "CANCELLED"
        assert kwargs["expected_status"] == "RUNNING"
        assert kwargs["files_edited"] == []
        assert kwargs["pr_url"] == ""
        assert kwargs["duration_seconds"] > 0

    @pytest.mark.asyncio
    async def test_cross_session_stop_failure_is_cancel_failed_and_no_ddb_write(self):
        """A generic StopRuntimeSession failure must NOT be reported as CANCELLED (Req 7.5).

        Nothing was stopped, so the record stays RUNNING and the caller is
        told exactly why the cancel did not happen.
        """
        job_id = "job-cross-fail-1"

        fake_record = {
            "job_id": job_id,
            "status": "RUNNING",
            "user_id": "user-1",
            "runtime_session_id": "sess-dead",
        }

        mock_boto_client = MagicMock()
        mock_boto_client.stop_runtime_session.side_effect = RuntimeError("session gone")

        with (
            patch(
                "container.code_mcp_server.query_job_record",
                new_callable=AsyncMock,
                return_value=fake_record,
            ),
            patch(
                "container.code_mcp_server.update_job_status",
                new_callable=AsyncMock,
            ) as mock_update,
            patch("container.code_mcp_server._get_runtime_arn", return_value=_ARN),
            patch("boto3.client", return_value=mock_boto_client),
        ):
            result = await cancel_task(job_id=job_id, _user_id="user-1")

        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert result["job_id"] == job_id
        assert "StopRuntimeSession failed: RuntimeError: session gone" in result["detail"]
        mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_session_session_already_terminated(self):
        """ResourceNotFoundException means the microVM is gone -> CANCELLED recorded."""
        job_id = "job-cross-gone"
        fake_record = {
            "job_id": job_id,
            "status": "RUNNING",
            "user_id": "user-1",
            "runtime_session_id": "00000000-0000-4000-8000-000000000000",
        }
        mock_boto_client = MagicMock()
        mock_boto_client.stop_runtime_session.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "Session 00000000-0000-4000-8000-000000000000 not found or has been terminated",
                }
            },
            "StopRuntimeSession",
        )

        with (
            patch(
                "container.code_mcp_server.query_job_record",
                new_callable=AsyncMock,
                return_value=fake_record,
            ),
            patch(
                "container.code_mcp_server.update_job_status",
                new_callable=AsyncMock,
            ) as mock_update,
            patch("container.code_mcp_server._get_runtime_arn", return_value=_ARN),
            patch("boto3.client", return_value=mock_boto_client),
        ):
            result = await cancel_task(job_id=job_id, _user_id="user-1")

        assert result["status"] == "CANCELLED"
        assert result["method"] == "session_already_terminated"
        mock_update.assert_awaited_once()
        assert mock_update.await_args.kwargs["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_cross_session_without_session_id_is_cancel_failed(self):
        """No runtime_session_id on the record -> nothing to stop, nothing written."""
        job_id = "job-cross-nosession"
        fake_record = {
            "job_id": job_id,
            "status": "RUNNING",
            "user_id": "user-1",
            "runtime_session_id": "",
        }
        mock_boto_client = MagicMock()

        with (
            patch(
                "container.code_mcp_server.query_job_record",
                new_callable=AsyncMock,
                return_value=fake_record,
            ),
            patch(
                "container.code_mcp_server.update_job_status",
                new_callable=AsyncMock,
            ) as mock_update,
            patch("container.code_mcp_server._get_runtime_arn", return_value=_ARN),
            patch("boto3.client", return_value=mock_boto_client),
        ):
            result = await cancel_task(job_id=job_id, _user_id="user-1")

        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert "no runtime_session_id" in result["detail"]
        mock_boto_client.stop_runtime_session.assert_not_called()
        mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_terminal_state_returns_error(self):
        """Verify cancelling a COMPLETE job returns error (Req 7.3)."""
        job_id = "job-done-1"

        with patch(
            "container.code_mcp_server.query_job_record",
            new_callable=AsyncMock,
            return_value={"job_id": job_id, "status": "COMPLETE", "user_id": "user-1"},
        ):
            result = await cancel_task(job_id=job_id, _user_id="user-1")

        assert "error" in result
        assert "terminal" in result["error"].lower()
