# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for _get_runtime_arn() and cancel_task ARN resolution.

Validates: Requirements 3.1, 3.2, 3.3, 3.4 (spec 18)
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from container.code_mcp_server import _get_runtime_arn, cancel_task


# ---------------------------------------------------------------------------
# _get_runtime_arn() unit tests
# ---------------------------------------------------------------------------


class TestGetRuntimeArn:
    """Unit tests for the _get_runtime_arn() helper."""

    def test_returns_runtime_arn_when_set(self):
        """_get_runtime_arn() returns RUNTIME_ARN when that env var is set.

        Validates: Requirements 3.1
        """
        with patch.dict(
            "os.environ",
            {"RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-1"},
            clear=False,
        ):
            result = _get_runtime_arn()

        assert result == "arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-1"

    def test_constructs_from_prefix_and_id(self):
        """_get_runtime_arn() constructs {PREFIX}{ID} when RUNTIME_ARN is unset.

        Validates: Requirements 3.2
        """
        env = {
            "RUNTIME_ARN_PREFIX": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/",
            "AGENT_RUNTIME_ID": "rt-abc123",
        }
        with patch.dict("os.environ", env, clear=False):
            # Ensure RUNTIME_ARN and OPENCODE_RUNTIME_ARN are not set
            with patch.dict(
                "os.environ",
                {"RUNTIME_ARN": "", "OPENCODE_RUNTIME_ARN": ""},
                clear=False,
            ):
                result = _get_runtime_arn()

        assert result == "arn:aws:bedrock-agentcore:us-east-1:123:runtime/rt-abc123"

    def test_returns_empty_when_nothing_set(self):
        """_get_runtime_arn() returns '' (and never calls AWS) when no env
        vars are set, including RUNTIME_NAME.

        Validates: Requirements 3.3
        """
        with (
            patch.dict(
                "os.environ",
                {
                    "RUNTIME_ARN": "",
                    "OPENCODE_RUNTIME_ARN": "",
                    "RUNTIME_ARN_PREFIX": "",
                    "AGENT_RUNTIME_ID": "",
                    "RUNTIME_NAME": "",
                },
                clear=False,
            ),
            patch("container.code_mcp_server.boto3.client") as mock_client,
        ):
            result = _get_runtime_arn()

        assert result == ""
        mock_client.assert_not_called()

    def test_discovers_arn_by_runtime_name(self):
        """With only RUNTIME_NAME set, the ARN is discovered via
        ListAgentRuntimes and cached for subsequent calls.
        """
        import container.code_mcp_server as server

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"agentRuntimes": [
                {"agentRuntimeName": "other", "agentRuntimeArn": "arn:other"},
            ]},
            {"agentRuntimes": [
                {"agentRuntimeName": "opencode_runtime",
                 "agentRuntimeArn": "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/rt-x"},
            ]},
        ]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        env = {
            "RUNTIME_ARN": "",
            "OPENCODE_RUNTIME_ARN": "",
            "RUNTIME_ARN_PREFIX": "",
            "AGENT_RUNTIME_ID": "",
            "RUNTIME_NAME": "opencode_runtime",
        }
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(server, "_discovered_runtime_arn", ""),
            patch("container.code_mcp_server.boto3.client", return_value=client) as mock_client,
        ):
            first = _get_runtime_arn()
            second = _get_runtime_arn()

        assert first == "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/rt-x"
        assert second == first
        # Cached after the first discovery: only one control-plane client built.
        assert mock_client.call_count == 1
        client.get_paginator.assert_called_once_with("list_agent_runtimes")

    def test_discovery_failure_returns_empty(self):
        """A control-plane error during discovery yields '' instead of raising."""
        import container.code_mcp_server as server

        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        env = {
            "RUNTIME_ARN": "",
            "OPENCODE_RUNTIME_ARN": "",
            "RUNTIME_ARN_PREFIX": "",
            "AGENT_RUNTIME_ID": "",
            "RUNTIME_NAME": "opencode_runtime",
        }
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(server, "_discovered_runtime_arn", ""),
            patch("container.code_mcp_server.boto3.client", return_value=client),
        ):
            assert _get_runtime_arn() == ""

    def test_falls_back_to_opencode_runtime_arn(self):
        """_get_runtime_arn() checks OPENCODE_RUNTIME_ARN as fallback.

        Validates: Requirements 2.2 (backward compatibility)
        """
        with patch.dict(
            "os.environ",
            {
                "RUNTIME_ARN": "",
                "OPENCODE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/legacy",
                "RUNTIME_ARN_PREFIX": "",
                "AGENT_RUNTIME_ID": "",
            },
            clear=False,
        ):
            result = _get_runtime_arn()

        assert result == "arn:aws:bedrock-agentcore:us-east-1:123:runtime/legacy"

    def test_runtime_arn_takes_precedence_over_opencode(self):
        """RUNTIME_ARN takes precedence over OPENCODE_RUNTIME_ARN.

        Validates: Requirements 3.1
        """
        with patch.dict(
            "os.environ",
            {
                "RUNTIME_ARN": "arn:primary",
                "OPENCODE_RUNTIME_ARN": "arn:legacy",
            },
            clear=False,
        ):
            result = _get_runtime_arn()

        assert result == "arn:primary"


# ---------------------------------------------------------------------------
# cancel_task ARN resolution tests
# ---------------------------------------------------------------------------


class TestCancelTaskArnResolution:
    """cancel_task logs warning and skips StopRuntimeSession when ARN is empty."""

    @pytest.mark.asyncio
    async def test_logs_warning_and_skips_stop_when_arn_empty(self, caplog):
        """cancel_task logs a warning and does not call StopRuntimeSession
        when _get_runtime_arn() returns ''. Nothing was stopped, so the
        result is ``cancel_failed`` and DynamoDB is not touched.

        Validates: Requirements 3.4
        """
        job_id = "job-no-arn"
        user_id = "user-1"
        session_id = "session-abc"

        async def mock_query(job_id, user_id):
            return {
                "job_id": job_id,
                "status": "RUNNING",
                "user_id": user_id,
                "runtime_session_id": session_id,
            }

        update_calls = []

        async def mock_update(job_id, user_id, status, **kwargs):
            update_calls.append(status)

        mock_client = MagicMock()

        with (
            patch(
                "container.code_mcp_server.query_job_record",
                side_effect=mock_query,
            ),
            patch(
                "container.code_mcp_server.update_job_status",
                side_effect=mock_update,
            ),
            patch(
                "container.code_mcp_server._get_runtime_arn",
                return_value="",
            ),
            patch(
                "boto3.client",
                return_value=mock_client,
            ),
            caplog.at_level(logging.WARNING, logger="container.code_mcp_server"),
        ):
            result = await cancel_task(job_id=job_id, _user_id=user_id)

        # StopRuntimeSession must NOT be called
        mock_client.stop_runtime_session.assert_not_called()

        # Warning must be logged
        assert any(
            "Cannot call StopRuntimeSession" in msg
            and job_id in msg
            for msg in caplog.messages
        ), f"Expected warning about unresolved ARN, got: {caplog.messages}"

        # Nothing was stopped: honest failure, record left untouched.
        assert result["error"] == "cancel_failed"
        assert result["status"] == "RUNNING"
        assert result["job_id"] == job_id
        assert "runtime ARN unresolved" in result["detail"]
        assert update_calls == []
