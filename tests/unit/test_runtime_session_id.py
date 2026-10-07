# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for runtime session id extraction from inbound headers.

Inside the container the Gateway -> Runtime request carries the AgentCore
runtime session id only as the ``session.id`` member of the W3C ``baggage``
header (observed platform behaviour). ``X-Amzn-Bedrock-AgentCore-Runtime-
Session-Id`` is checked first for forward compatibility.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from container.code_mcp_server import (
    _current_runtime_session_id,
    _runtime_session_id_from_headers,
)

_SID = "f67ddcd1-dc44-4867-8793-e4888e672b6b"

# Exact header set captured inside the container on a Gateway tools/call.
_REAL_HEADERS = {
    "accept": "application/json, text/event-stream",
    "baggage": f"Self=1-6ac63484-1db2fd2a16a5f40167a87b9e,session.id={_SID}",
    "content-length": "412",
    "content-type": "application/json; charset=utf-8",
    "host": "127.0.0.1:8000",
    "mcp-protocol-version": "2025-06-18",
    "mcp-session-id": "cd71ed7815484b5e902786a3c0083d22",
    "x-amzn-requestid": "94538990-ce54-40a4-a303-6e0b7080aed7",
    "x-amzn-trace-id": "Root=1-6ac63484-1db2fd2a16a5f40167a87b9e",
}


class TestRuntimeSessionIdFromHeaders:
    def test_real_captured_headers(self):
        assert _runtime_session_id_from_headers(_REAL_HEADERS) == _SID

    def test_baggage_with_self_prefix(self):
        headers = {"baggage": f"Self=1-abc-def,session.id={_SID}"}
        assert _runtime_session_id_from_headers(headers) == _SID

    def test_baggage_with_only_session_id(self):
        assert _runtime_session_id_from_headers({"baggage": f"session.id={_SID}"}) == _SID

    def test_baggage_session_id_first_then_other_members(self):
        headers = {"baggage": f"session.id={_SID},Self=1-abc,foo=bar"}
        assert _runtime_session_id_from_headers(headers) == _SID

    def test_baggage_property_suffix_is_dropped(self):
        headers = {"baggage": f"Self=1-abc,session.id={_SID};prop=1;other"}
        assert _runtime_session_id_from_headers(headers) == _SID

    def test_baggage_whitespace_around_members(self):
        headers = {"baggage": f" Self=1-abc , session.id = {_SID} "}
        assert _runtime_session_id_from_headers(headers) == _SID

    def test_mcp_session_id_is_never_used(self):
        """The MCP transport session id is not the runtime session id."""
        headers = {"mcp-session-id": "cd71ed7815484b5e902786a3c0083d22"}
        assert _runtime_session_id_from_headers(headers) == ""

    def test_x_amzn_header_wins_over_baggage(self):
        headers = {
            "x-amzn-bedrock-agentcore-runtime-session-id": "direct-session-id-value-1234567890",
            "baggage": f"session.id={_SID}",
        }
        assert _runtime_session_id_from_headers(headers) == (
            "direct-session-id-value-1234567890"
        )

    def test_mixed_case_header_names(self):
        headers = {
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "direct-session-id-value-1234567890",
        }
        assert _runtime_session_id_from_headers(headers) == (
            "direct-session-id-value-1234567890"
        )
        assert _runtime_session_id_from_headers({"Baggage": f"session.id={_SID}"}) == _SID

    def test_empty_x_amzn_header_falls_back_to_baggage(self):
        headers = {
            "x-amzn-bedrock-agentcore-runtime-session-id": "   ",
            "baggage": f"session.id={_SID}",
        }
        assert _runtime_session_id_from_headers(headers) == _SID

    def test_no_headers_returns_empty(self):
        assert _runtime_session_id_from_headers({}) == ""

    def test_baggage_without_session_id_returns_empty(self):
        assert _runtime_session_id_from_headers({"baggage": "Self=1-abc,foo=bar"}) == ""

    def test_malformed_baggage_members_are_skipped(self):
        headers = {"baggage": f",,novalue,=,session.id={_SID},"}
        assert _runtime_session_id_from_headers(headers) == _SID

    def test_empty_session_id_value_returns_empty(self):
        assert _runtime_session_id_from_headers({"baggage": "session.id="}) == ""
        assert _runtime_session_id_from_headers({"baggage": "session.id=;prop"}) == ""


class TestCurrentRuntimeSessionId:
    def test_reads_headers_with_include_all(self):
        with patch(
            "container.code_mcp_server.get_http_headers", return_value=_REAL_HEADERS
        ) as mock_headers:
            assert _current_runtime_session_id() == _SID
        mock_headers.assert_called_once_with(include_all=True)

    def test_warns_and_returns_empty_when_missing(self, caplog):
        headers = {"accept": "application/json", "mcp-session-id": "abc"}
        with (
            patch("container.code_mcp_server.get_http_headers", return_value=headers),
            caplog.at_level(logging.WARNING, logger="container.code_mcp_server"),
        ):
            assert _current_runtime_session_id() == ""
        assert any(
            "No AgentCore runtime session id" in msg
            and "cross-session cancel will be unavailable" in msg
            and "mcp-session-id" in msg
            for msg in caplog.messages
        ), caplog.messages

    def test_never_raises_when_get_http_headers_fails(self, caplog):
        with (
            patch(
                "container.code_mcp_server.get_http_headers",
                side_effect=RuntimeError("no request context"),
            ),
            caplog.at_level(logging.WARNING, logger="container.code_mcp_server"),
        ):
            assert _current_runtime_session_id() == ""

    def test_handles_none_return(self):
        with patch("container.code_mcp_server.get_http_headers", return_value=None):
            assert _current_runtime_session_id() == ""
