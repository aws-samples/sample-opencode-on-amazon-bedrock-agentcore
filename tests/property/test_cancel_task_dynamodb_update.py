# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: cancel_task records CANCELLED iff something was actually stopped.

Feature: 13-runtime-consolidation
Property 1 (revised): DynamoDB is updated to CANCELLED **iff**
StopRuntimeSession succeeded or reported ResourceNotFoundException
(session already terminated). It is NEVER updated when StopRuntimeSession
fails for any other reason, and NEVER when the job record carries no
``runtime_session_id`` (there is nothing to stop). In every "not stopped"
case the response is ``{"error": "cancel_failed", "status": "RUNNING", ...}``.

Validates: Requirements 6.2, 6.3
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

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
# Non-empty session ids (whitespace-free so the API-shaped mock sees them as-is).
session_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=60,
)
created_at_st = st.sampled_from([
    "2024-01-01T00:00:00+00:00",   # parseable, aware
    "2024-01-01T00:00:00",         # parseable, naive (treated as UTC)
    "",                            # missing
    "garbage",                     # unparseable -> 0.0
])

stop_outcome_st = st.sampled_from([
    "success",
    "resource_not_found",
    "other_client_error",
    "generic_exception",
])


def _stop_side_effect(outcome: str):
    if outcome == "success":
        return None
    if outcome == "resource_not_found":
        return ClientError(
            {"Error": {"Code": "ResourceNotFoundException",
                       "Message": "Session x not found or has been terminated"}},
            "StopRuntimeSession",
        )
    if outcome == "other_client_error":
        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "StopRuntimeSession",
        )
    return RuntimeError("StopRuntimeSession failed")


class TestCancelTaskWritesCancelledIffStopped:
    """**Feature: 13-runtime-consolidation, Property 1 (revised)**"""

    @given(
        job_id=job_id_st,
        user_id=user_id_st,
        session_id=session_id_st,
        created_at=created_at_st,
        outcome=stop_outcome_st,
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_write_iff_stop_succeeded_or_session_gone(
        self, job_id, user_id, session_id, created_at, outcome
    ):
        """**Validates: Requirements 6.2, 6.3**"""
        update_calls = []

        async def mock_query_job_record(job_id, user_id):
            return {
                "job_id": job_id,
                "status": "RUNNING",
                "user_id": user_id,
                "runtime_session_id": session_id,
                "created_at": created_at,
            }

        async def mock_update_job_status(job_id, user_id, status, **kwargs):
            update_calls.append({"job_id": job_id, "user_id": user_id, "status": status, **kwargs})

        _running_tasks.pop(job_id, None)
        _cancel_flags.pop(job_id, None)

        mock_boto_client = MagicMock()
        mock_boto_client.stop_runtime_session = MagicMock(
            side_effect=_stop_side_effect(outcome)
        )
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_boto_client

        with (
            patch("container.code_mcp_server.query_job_record", side_effect=mock_query_job_record),
            patch("container.code_mcp_server.update_job_status", side_effect=mock_update_job_status),
            patch(
                "container.code_mcp_server._get_runtime_arn",
                return_value="arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-test",
            ),
            patch("container.code_mcp_server.boto3", mock_boto3),
        ):
            result = await cancel_task(job_id=job_id, _user_id=user_id)

        mock_boto_client.stop_runtime_session.assert_called_once()
        assert result["job_id"] == job_id

        if outcome in ("success", "resource_not_found"):
            assert len(update_calls) == 1, update_calls
            w = update_calls[0]
            assert w["status"] == "CANCELLED"
            assert w["job_id"] == job_id and w["user_id"] == user_id
            assert w["expected_status"] == "RUNNING"
            assert w["files_edited"] == []
            assert w["pr_url"] == ""
            assert w["stop_reason"] == ""
            assert w["error"] == "Task cancelled by user"
            assert isinstance(w["duration_seconds"], float) and w["duration_seconds"] >= 0.0
            assert "completed_at" in w
            assert result["status"] == "CANCELLED"
            assert result["method"] == (
                "stop_runtime_session" if outcome == "success" else "session_already_terminated"
            )
        else:
            assert update_calls == [], update_calls
            assert result["error"] == "cancel_failed"
            assert result["status"] == "RUNNING"
            assert "StopRuntimeSession failed" in result["detail"]

    @given(job_id=job_id_st, user_id=user_id_st, outcome=stop_outcome_st)
    @settings(max_examples=50)
    @pytest.mark.asyncio
    async def test_never_writes_without_runtime_session_id(self, job_id, user_id, outcome):
        """**Validates: Requirements 6.2, 6.3** - empty session id: no stop, no write."""
        update_calls = []

        async def mock_query_job_record(job_id, user_id):
            return {
                "job_id": job_id,
                "status": "RUNNING",
                "user_id": user_id,
                "runtime_session_id": "",
            }

        async def mock_update_job_status(job_id, user_id, status, **kwargs):
            update_calls.append(status)

        _running_tasks.pop(job_id, None)
        _cancel_flags.pop(job_id, None)

        mock_boto_client = MagicMock()
        mock_boto_client.stop_runtime_session = MagicMock(
            side_effect=_stop_side_effect(outcome)
        )
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_boto_client

        with (
            patch("container.code_mcp_server.query_job_record", side_effect=mock_query_job_record),
            patch("container.code_mcp_server.update_job_status", side_effect=mock_update_job_status),
            patch(
                "container.code_mcp_server._get_runtime_arn",
                return_value="arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-test",
            ),
            patch("container.code_mcp_server.boto3", mock_boto3),
        ):
            result = await cancel_task(job_id=job_id, _user_id=user_id)

        mock_boto_client.stop_runtime_session.assert_not_called()
        assert update_calls == []
        assert result == {
            "job_id": job_id,
            "status": "RUNNING",
            "error": "cancel_failed",
            "detail": "job record has no runtime_session_id; cannot locate the microVM running it",
        }
