# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for run_opencode_acp tool."""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from container.tools.run_opencode_acp import (
    OpenCodeResult,
    _build_opencode_config,
    _build_spawn_env,
    _collect_edited_paths,
    _drain_stderr,
    _make_jsonrpc,
    _parse_prompt_usage,
    _relativize,
    _selected_model,
    _terminate_process,
    run_opencode_acp,
)

# Fake pid used by ``_mock_proc``. ``os.killpg`` is always patched (see the
# autouse fixture below) so no real process group is ever signalled, except
# by the real-subprocess test, which restores the original explicitly.
_FAKE_PID = 424242
_REAL_KILLPG = os.killpg


@pytest.fixture(autouse=True)
def mock_killpg():
    """Patch ``os.killpg`` for every test in this module.

    The default behaviour models a process group that is already gone
    (``ProcessLookupError``), which is what the real call returns once
    OpenCode and its children have exited.
    """
    with patch(
        "container.tools.run_opencode_acp.os.killpg",
        side_effect=ProcessLookupError,
    ) as killpg:
        yield killpg


class TestMakeJsonrpc:
    def test_basic_message(self):
        result = _make_jsonrpc(1, "initialize", {"protocolVersion": "1.0"})
        parsed = json.loads(result)
        assert parsed == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1.0"},
        }

    def test_ends_with_newline(self):
        result = _make_jsonrpc(1, "test", {})
        assert result.endswith("\n")


class TestBuildOpenCodeConfig:
    def test_shape(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_MODEL", "global.anthropic.claude-opus-4-6-v1")
        config = _build_opencode_config()
        assert config["model"] == "amazon-bedrock/global.anthropic.claude-opus-4-6-v1"
        assert config["autoupdate"] is False
        assert "opencode" in config["disabled_providers"]
        assert config["permission"]["edit"] == "allow"
        assert config["permission"]["bash"] == "allow"

    def test_default_model(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_MODEL", raising=False)
        config = _build_opencode_config()
        assert config["model"] == (
            "amazon-bedrock/global.anthropic.claude-opus-4-6-v1"
        )


class TestBuildSpawnEnv:
    def test_sets_autoupdate_disable_flag(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENCODE_MODEL", raising=False)
        env = _build_spawn_env(str(tmp_path))
        # AUTOUPDATE is the only DISABLE_* flag that has been proven
        # necessary — the microVM has a fresh filesystem on every cold
        # start and autoupdate would attempt to download a new OpenCode
        # binary each time.
        assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "true"

    def test_config_passed_inline_via_env(self, tmp_path, monkeypatch):
        """Config is passed inline via OPENCODE_CONFIG_CONTENT, not a file."""
        monkeypatch.delenv("OPENCODE_MODEL", raising=False)
        env = _build_spawn_env(str(tmp_path))
        assert "OPENCODE_CONFIG_CONTENT" in env
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["model"].startswith("amazon-bedrock/")
        assert config.get("autoupdate") is False
        # A path-based config must NOT be set (it would let an on-disk
        # project opencode.json override the inline config).
        assert "OPENCODE_CONFIG" not in env

    def test_inherited_opencode_config_path_stripped(self, tmp_path, monkeypatch):
        """A pre-set OPENCODE_CONFIG path must not reach the child env.

        ``_build_spawn_env`` copies ``os.environ``; if an OPENCODE_CONFIG
        path is already set in the parent process it would otherwise be
        inherited alongside the inline content and act as a separate config
        source, violating the no-path requirement.
        """
        monkeypatch.delenv("OPENCODE_MODEL", raising=False)
        monkeypatch.setenv("OPENCODE_CONFIG", "/some/inherited/opencode.json")
        env = _build_spawn_env(str(tmp_path))
        assert "OPENCODE_CONFIG" not in env
        assert "OPENCODE_CONFIG_CONTENT" in env

    def test_aws_creds_passed_through(self, tmp_path, monkeypatch):
        """AWS creds resolved from boto3 are set on the spawn env."""
        import importlib
        mod = importlib.import_module("container.tools.run_opencode_acp")

        def _fake_resolve():
            return {
                "AWS_ACCESS_KEY_ID": "AKIA-FAKE",
                "AWS_SECRET_ACCESS_KEY": "FAKE-SECRET",
                "AWS_SESSION_TOKEN": "FAKE-SESSION-TOKEN",
            }

        monkeypatch.setattr(mod, "_resolve_aws_credentials_into_env", _fake_resolve)
        env = _build_spawn_env(str(tmp_path))
        assert env["AWS_ACCESS_KEY_ID"] == "AKIA-FAKE"
        assert env["AWS_SECRET_ACCESS_KEY"] == "FAKE-SECRET"
        assert env["AWS_SESSION_TOKEN"] == "FAKE-SESSION-TOKEN"


def _make_acp_response(id: int, result: dict) -> bytes:
    """Helper to create a JSON-RPC response line."""
    return (json.dumps({"jsonrpc": "2.0", "id": id, "result": result}) + "\n").encode()


def _make_acp_notification(method: str, params: dict) -> bytes:
    """Helper to create a JSON-RPC notification line."""
    return (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode()


def _make_acp_request(id, method: str, params: dict) -> bytes:
    """Helper to create a JSON-RPC request line sent by the agent to us."""
    return (json.dumps({
        "jsonrpc": "2.0", "id": id, "method": method, "params": params,
    }) + "\n").encode()


def _written_messages(proc) -> list[dict]:
    """Decode every newline-delimited JSON message written to ``proc.stdin``."""
    data = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def _mock_proc(stdout_lines: list[bytes], returncode: int = 0, stderr: bytes = b""):
    """Create a mock async subprocess with given stdout lines.

    Note: ``returncode`` sets the value on the mock immediately (as if the
    process has already exited). Callers that want to exercise the
    "process alive while reading" path should leave it at the default 0
    and rely on stdout EOF to trigger the loop exit.
    """
    proc = AsyncMock()
    proc.pid = _FAKE_PID
    proc.returncode = returncode
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(b"".join(stdout_lines))
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_data(stderr)
    proc.stderr.feed_eof()
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestRunOpenCodeAcp:
    """Tests for the run_opencode_acp function."""

    @pytest.mark.asyncio
    async def test_successful_execution(self, tmp_path):
        """Test a successful ACP protocol exchange."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "sess-123"}),
            _make_acp_notification("session/update", {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "Editing file.py"},
                },
            }),
            # Legacy (pre-1.18) location shape: ``uri`` instead of ``path``.
            _make_acp_notification("session/update", {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "legacy-1",
                    "title": "Edit file.py",
                    "kind": "edit",
                    "locations": [{"uri": "file.py"}],
                },
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        assert result["stop_reason"] == "end_turn"
        assert "Editing file.py" in result["stdout"]
        assert result["files_edited"] == ["file.py"]
        # No usage block in the response -> all-zero usage dict.
        assert result["usage"]["total_tokens"] == 0
        assert result["usage"]["prompt_tokens"] == 0

    @pytest.mark.asyncio
    async def test_no_opencode_json_written_to_tree(self, tmp_path):
        """A successful run must not write opencode.json into the work tree."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "sess-cfg"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        assert result["stop_reason"] == "end_turn"
        assert not (tmp_path / "opencode.json").exists()

    @pytest.mark.asyncio
    async def test_acp_error_response(self, tmp_path):
        """Test handling of an ACP error response."""
        error_line = (json.dumps({
            "jsonrpc": "2.0", "id": 3,
            "error": {"code": -1, "message": "Model overloaded"},
        }) + "\n").encode()

        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "sess-456"}),
            error_line,
        ], returncode=1, stderr=b"error output")

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            with pytest.raises(RuntimeError, match="Model overloaded"):
                await run_opencode_acp(
                    work_dir=str(tmp_path),
                    task_description="Fix the bug",
                    timeout_seconds=60,
                )

    @pytest.mark.asyncio
    async def test_no_session_id_raises(self, tmp_path):
        """Test that missing sessionId raises RuntimeError."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {}),  # No sessionId
        ], returncode=1)

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            with pytest.raises(RuntimeError, match="No sessionId"):
                await run_opencode_acp(
                    work_dir=str(tmp_path),
                    task_description="Fix the bug",
                    timeout_seconds=60,
                )

    @pytest.mark.asyncio
    async def test_multiple_progress_notifications(self, tmp_path):
        """Multiple agent_message_chunk notifications are collected in order."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "sess-789"}),
            _make_acp_notification("session/update", {
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "Reading main.py"}},
            }),
            _make_acp_notification("session/update", {
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "Editing utils.py"}},
            }),
            _make_acp_notification("session/update", {
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "Running tests"}},
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Refactor code",
                timeout_seconds=120,
            )

        assert result["stdout"] == "Reading main.py\nEditing utils.py\nRunning tests"
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_positive_nonzero_exit_code_raises(self, tmp_path):
        """Positive non-zero exit codes still indicate a real failure.

        Negative codes (signals, e.g. -15 from our own SIGTERM cleanup)
        are tolerated — see ``test_negative_exit_code_tolerated``.
        """
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "s1"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ], returncode=137, stderr=b"killed by OOM")

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            with pytest.raises(RuntimeError, match="exited with code 137"):
                await run_opencode_acp(
                    work_dir=str(tmp_path),
                    task_description="Fix bug",
                    timeout_seconds=60,
                )

    @pytest.mark.asyncio
    async def test_negative_exit_code_tolerated(self, tmp_path):
        """A negative return code (our own SIGTERM) must not fail the run.

        After we read the final ``stopReason`` and break out of the loop,
        the ``finally`` block terminates the still-running process. The
        resulting -15 return code is expected cleanup, not a failure.
        """
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "s1"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ], returncode=-15)

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix bug",
                timeout_seconds=60,
            )

        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_permission_request_rejected_once_and_run_continues(self, tmp_path):
        """A mid-stream session/request_permission is answered with reject_once.

        The run must keep going and end on the real session/prompt response.
        """
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "sess-perm"}),
            _make_acp_request(0, "session/request_permission", {
                "sessionId": "sess-perm",
                "toolCall": {"toolCallId": "call-1", "title": "rm -rf build"},
                "options": [
                    {"optionId": "opt-allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "opt-allow-all", "name": "Always", "kind": "allow_always"},
                    {"optionId": "opt-reject", "name": "Reject", "kind": "reject_once"},
                ],
            }),
            _make_acp_notification("session/update", {
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"text": "after permission"}},
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        responses = [m for m in _written_messages(proc) if "method" not in m]
        assert responses == [{
            "jsonrpc": "2.0",
            "id": 0,
            "result": {"outcome": {"outcome": "selected", "optionId": "opt-reject"}},
        }]
        assert result["stop_reason"] == "end_turn"
        assert "after permission" in result["stdout"]
        # The request itself must not leak into collected stdout.
        assert "request_permission" not in result["stdout"]

    @pytest.mark.asyncio
    async def test_permission_request_falls_back_to_reject_always(self, tmp_path):
        """With no reject_once option, reject_always is selected before cancelled."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "sess-perm2"}),
            _make_acp_request("perm-7", "session/request_permission", {
                "sessionId": "sess-perm2",
                "toolCall": {"toolCallId": "call-2", "title": "Edit x.py"},
                "options": [
                    {"optionId": "a1", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "r-all", "name": "Never", "kind": "reject_always"},
                ],
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        responses = [m for m in _written_messages(proc) if "method" not in m]
        assert responses == [{
            "jsonrpc": "2.0",
            "id": "perm-7",
            "result": {"outcome": {"outcome": "selected", "optionId": "r-all"}},
        }]
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_permission_request_without_reject_option_cancelled(self, tmp_path):
        """With no reject option at all, the permission outcome is cancelled."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "sess-perm2"}),
            _make_acp_request("perm-7", "session/request_permission", {
                "sessionId": "sess-perm2",
                "toolCall": {"toolCallId": "call-2", "title": "Edit x.py"},
                "options": [
                    {"optionId": "a1", "name": "Allow", "kind": "allow_once"},
                ],
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        responses = [m for m in _written_messages(proc) if "method" not in m]
        assert responses == [{
            "jsonrpc": "2.0",
            "id": "perm-7",
            "result": {"outcome": {"outcome": "cancelled"}},
        }]
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_agent_request_with_prompt_id_not_mistaken_for_response(self, tmp_path):
        """An agent request reusing id 3 is answered, not treated as the prompt reply.

        Only the later id-3 message without a ``method`` ends the loop.
        """
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "sess-id3"}),
            _make_acp_request(3, "session/request_permission", {
                "sessionId": "sess-id3",
                "toolCall": {"toolCallId": "call-3", "title": "Run tests"},
                "options": [
                    {"optionId": "rej", "name": "Reject", "kind": "reject_once"},
                ],
            }),
            _make_acp_notification("session/update", {
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"text": "still working"}},
            }),
            _make_acp_response(3, {"stopReason": "max_tokens"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        responses = [m for m in _written_messages(proc) if "method" not in m]
        assert responses == [{
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"outcome": {"outcome": "selected", "optionId": "rej"}},
        }]
        # stopReason comes from the real response, and the notification
        # between the two id-3 messages was still processed.
        assert result["stop_reason"] == "max_tokens"
        assert "still working" in result["stdout"]

    @pytest.mark.asyncio
    async def test_unknown_agent_request_gets_method_not_found(self, tmp_path):
        """Unsupported agent requests get a JSON-RPC -32601 error with the same id."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "sess-unk"}),
            _make_acp_request(42, "fs/read_text_file", {
                "sessionId": "sess-unk", "path": "/etc/passwd",
            }),
            _make_acp_request(43, "terminal/create", {
                "sessionId": "sess-unk", "command": "ls",
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        responses = [m for m in _written_messages(proc) if "method" not in m]
        assert responses == [
            {"jsonrpc": "2.0", "id": 42,
             "error": {"code": -32601, "message": "Method not found"}},
            {"jsonrpc": "2.0", "id": 43,
             "error": {"code": -32601, "message": "Method not found"}},
        ]
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_handshake_skips_method_messages(self, tmp_path):
        """Messages with a ``method`` before init/session-new replies are not
        mistaken for those replies; agent requests there are answered."""
        proc = _mock_proc([
            _make_acp_notification("session/update", {"update": {}}),
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_request(2, "fs/read_text_file", {"path": "x"}),
            _make_acp_response(2, {"sessionId": "sess-hs"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix the bug",
                timeout_seconds=60,
            )

        written = _written_messages(proc)
        assert [m.get("method") for m in written if "method" in m] == [
            "initialize", "session/new", "session/prompt",
        ]
        prompt = next(m for m in written if m.get("method") == "session/prompt")
        assert prompt["params"]["sessionId"] == "sess-hs"
        responses = [m for m in written if "method" not in m]
        assert responses == [{
            "jsonrpc": "2.0", "id": 2,
            "error": {"code": -32601, "message": "Method not found"},
        }]
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_eof_before_init_response(self, tmp_path):
        """Test that EOF before initialize response raises RuntimeError."""
        proc = _mock_proc([], returncode=1)

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc):
            with pytest.raises(RuntimeError, match="closed stdout before initialize"):
                await run_opencode_acp(
                    work_dir=str(tmp_path),
                    task_description="Fix bug",
                    timeout_seconds=60,
                )

    @pytest.mark.asyncio
    async def test_nonzero_exit_reports_stderr_tail(self, tmp_path):
        """On failure the error carries the end of stderr, not the start."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": "1.0"}),
            _make_acp_response(2, {"sessionId": "s1"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ], returncode=1)

        def _fill_stderr(_proc, buffer):
            # Fill the buffer synchronously: with mocked stdout the read
            # loop finishes before a real drain task would get scheduled.
            buffer.extend(["HEAD_MARKER"] + ["x" * 100] * 30 + ["TAIL_MARKER"])
            return asyncio.sleep(0)

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                    return_value=proc), \
             patch("container.tools.run_opencode_acp._drain_stderr", _fill_stderr):
            with pytest.raises(RuntimeError) as exc_info:
                await run_opencode_acp(
                    work_dir=str(tmp_path),
                    task_description="Fix bug",
                    timeout_seconds=60,
                )

        message = str(exc_info.value)
        assert "stderr tail" in message
        assert "TAIL_MARKER" in message
        assert "HEAD_MARKER" not in message


# ---------------------------------------------------------------------------
# Process-group termination
# ---------------------------------------------------------------------------


def _recording_killpg():
    """Build a fake ``os.killpg`` that records calls and always succeeds."""
    calls: list[tuple[int, int]] = []

    def _killpg(pgid, sig):
        calls.append((pgid, sig))
        return None

    return _killpg, calls


class TestProcessGroupTermination:
    """OpenCode runs in its own process group and the whole group is killed."""

    @pytest.mark.asyncio
    async def test_spawned_in_new_session(self, tmp_path):
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "s-pg"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                   return_value=proc) as spawn:
            await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix bug",
                timeout_seconds=60,
            )

        assert spawn.call_args.kwargs["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_group_killed_even_when_leader_already_exited(
        self, tmp_path, mock_killpg
    ):
        """A successful run still SIGKILLs the group before returning, so a
        background child of OpenCode cannot outlive the call."""
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {"sessionId": "s-pg"}),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ], returncode=0)

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                   return_value=proc):
            result = await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix bug",
                timeout_seconds=60,
            )

        assert result["stop_reason"] == "end_turn"
        assert call(_FAKE_PID, signal.SIGKILL) in mock_killpg.call_args_list
        # Leader had already exited, so no SIGTERM was needed.
        assert call(_FAKE_PID, signal.SIGTERM) not in mock_killpg.call_args_list

    @pytest.mark.asyncio
    async def test_running_leader_gets_sigterm_then_group_sigkill(self, mock_killpg):
        killpg, calls = _recording_killpg()
        mock_killpg.side_effect = killpg
        proc = _mock_proc([], returncode=None)
        proc.wait = AsyncMock(return_value=-15)

        await _terminate_process(proc)

        assert calls == [
            (_FAKE_PID, signal.SIGTERM),
            (_FAKE_PID, signal.SIGKILL),
        ]
        proc.send_signal.assert_not_called()
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_sigkill_escalation_after_grace(self, mock_killpg):
        killpg, calls = _recording_killpg()
        mock_killpg.side_effect = killpg
        proc = _mock_proc([], returncode=None)
        waits = {"n": 0}

        async def _wait():
            waits["n"] += 1
            if waits["n"] == 1:
                await asyncio.sleep(10)  # ignores SIGTERM
            return -9

        proc.wait = _wait

        with patch("container.tools.run_opencode_acp._TERM_GRACE_SECONDS", 0.01):
            await _terminate_process(proc)

        assert calls == [
            (_FAKE_PID, signal.SIGTERM),
            (_FAKE_PID, signal.SIGKILL),  # escalation after the grace period
            (_FAKE_PID, signal.SIGKILL),  # final sweep of the group
        ]

    @pytest.mark.asyncio
    async def test_process_lookup_error_is_tolerated(self, mock_killpg):
        """A group that is already gone is not an error."""
        proc = _mock_proc([], returncode=None)

        await _terminate_process(proc)

        assert mock_killpg.call_args_list == [
            call(_FAKE_PID, signal.SIGTERM),
            call(_FAKE_PID, signal.SIGKILL),
        ]

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    async def test_real_background_child_is_killed(self, mock_killpg):
        """With a real subprocess in its own session, a background child that
        outlives the leader's normal exit path is gone after termination."""
        mock_killpg.side_effect = _REAL_KILLPG
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 60 & echo $!; exec sleep 60",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        child_pid = int((await proc.stdout.readline()).decode().strip())

        await _terminate_process(proc)

        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "background child survived"
            await asyncio.sleep(0.05)
        assert proc.returncode is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pid", [None, 0, 1, -5])
    async def test_unexpected_pid_never_signals_a_group(self, mock_killpg, pid):
        """pgid 0 would be our own process group; never signal it."""
        proc = _mock_proc([], returncode=None)
        proc.pid = pid

        await _terminate_process(proc)

        mock_killpg.assert_not_called()


# ---------------------------------------------------------------------------
# stderr log level
# ---------------------------------------------------------------------------


def _stderr_proc(data: bytes):
    proc = MagicMock()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_data(data)
    proc.stderr.feed_eof()
    return proc


class TestOpenCodeStderrLogLevel:
    """OpenCode stderr is logged at INFO and kept for failure diagnostics."""

    _LOGGER = "container.tools.run_opencode_acp"

    @pytest.mark.asyncio
    async def test_stderr_lines_logged_at_info(self, caplog):
        proc = _stderr_proc(b"STDERR_MARKER line one\nSTDERR_MARKER line two\n")
        buffer: list[str] = []
        with caplog.at_level(logging.INFO, logger=self._LOGGER):
            await _drain_stderr(proc, buffer)
        assert buffer == ["STDERR_MARKER line one", "STDERR_MARKER line two"]
        records = [r for r in caplog.records if "STDERR_MARKER" in r.getMessage()]
        assert len(records) == 2
        assert all(r.levelno == logging.INFO for r in records)


# ---------------------------------------------------------------------------
# Selected model in the session/new response
# ---------------------------------------------------------------------------

_MODEL = "amazon-bedrock/global.anthropic.claude-opus-4-6-v1"


class TestSelectedModel:
    def test_opencode_1_18_config_options_shape(self):
        # Shape of a real OpenCode 1.18.34 session/new result: the model is
        # a configOptions entry and _meta is empty.
        result = {
            "sessionId": "ses_1",
            "configOptions": [
                {"id": "mode", "currentValue": "build"},
                {"id": "model", "currentValue": _MODEL, "options": []},
            ],
            "_meta": {},
        }
        assert _selected_model(result) == _MODEL

    def test_legacy_meta_shape(self):
        result = {"sessionId": "ses_1", "_meta": {"opencode": {"modelId": _MODEL}}}
        assert _selected_model(result) == _MODEL

    def test_config_options_preferred_over_meta(self):
        result = {
            "configOptions": [{"id": "model", "currentValue": _MODEL}],
            "_meta": {"opencode": {"modelId": "other"}},
        }
        assert _selected_model(result) == _MODEL

    @pytest.mark.parametrize("result", [
        {},
        {"configOptions": [], "_meta": {}},
        {"configOptions": [{"id": "model"}]},
        {"configOptions": "model", "_meta": []},
        {"configOptions": [None, "x", {"id": "model", "currentValue": 5}]},
        None,
    ])
    def test_unknown_when_absent_or_malformed(self, result):
        assert _selected_model(result) == "unknown"

    @pytest.mark.asyncio
    async def test_model_logged_from_config_options(self, tmp_path, caplog):
        proc = _mock_proc([
            _make_acp_response(1, {"protocolVersion": 1}),
            _make_acp_response(2, {
                "sessionId": "ses_m",
                "configOptions": [{"id": "model", "currentValue": _MODEL}],
                "_meta": {},
            }),
            _make_acp_response(3, {"stopReason": "end_turn"}),
        ])

        with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
                   return_value=proc), \
             caplog.at_level(logging.INFO, logger="container.tools.run_opencode_acp"):
            await run_opencode_acp(
                work_dir=str(tmp_path),
                task_description="Fix bug",
                timeout_seconds=60,
            )

        assert any(
            "session created" in r.getMessage() and f"model={_MODEL}" in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# OpenCode 1.18.34 ACP payloads (captured live 2026-10-07, job 44cb2a5c)
# ---------------------------------------------------------------------------

# Placeholder for the job work_dir; substituted with the test's tmp_path.
_WORK_DIR = "/tmp/opencode-sessions/<job>"
_TOOL_CALL_ID = "tooluse_Ll8EXBWv9ThqsVDxm8QOXE"

# Exact session/update and response shapes emitted by ``opencode acp``
# 1.18.34 for the task "Create a file TEST-1.md containing one line".
# Only file paths are included; no user data.
ACP_1_18_34_FIXTURE_LINES: list[tuple[str, object]] = [
    ("response", (1, {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": True},
        "agentInfo": {"name": "opencode", "version": "1.18.34"},
    })),
    ("response", (2, {
        "sessionId": "ses_fixture",
        "configOptions": [
            {"id": "mode", "currentValue": "build"},
            {"id": "model", "currentValue": _MODEL, "options": []},
        ],
        "_meta": {},
    })),
    ("update", {
        "sessionUpdate": "available_commands_update",
        "availableCommands": [
            {"name": "customize-opencode", "description": "..."},
            {"name": "init", "description": "..."},
            {"name": "review", "description": "..."},
        ],
    }),
    ("update", {
        "sessionUpdate": "tool_call",
        "toolCallId": _TOOL_CALL_ID,
        "title": "write",
        "kind": "edit",
        "status": "pending",
        "locations": [],
        "rawInput": {},
    }),
    ("update", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": _TOOL_CALL_ID,
        "status": "in_progress",
        "kind": "edit",
        "title": "write",
        "locations": [{"path": f"{_WORK_DIR}/TEST-1.md"}],
        "rawInput": {"filePath": f"{_WORK_DIR}/TEST-1.md", "content": "diag test 1\n"},
    }),
    ("update", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": _TOOL_CALL_ID,
        "status": "completed",
        "title": "TEST-1.md",
        "content": [{"type": "content",
                     "content": {"type": "text", "text": "Wrote file successfully."}}],
        "rawOutput": {
            "output": "Wrote file successfully.",
            "metadata": {
                "diagnostics": {},
                "filepath": f"{_WORK_DIR}/TEST-1.md",
                "exists": False,
                "truncated": False,
            },
        },
    }),
    ("update", {"sessionUpdate": "agent_message_chunk", "messageId": "msg_1",
                "content": {"type": "text", "text": "Created"}}),
    ("update", {"sessionUpdate": "agent_message_chunk", "messageId": "msg_1",
                "content": {"type": "text", "text": " TEST-1.md"}}),
    ("update", {"sessionUpdate": "agent_message_chunk", "messageId": "msg_1",
                "content": {"type": "text", "text": "."}}),
    ("update", {
        "sessionUpdate": "usage_update",
        "used": 8119,
        "size": 1000000,
        "cost": {"amount": 0.058151999999999995, "currency": "USD"},
    }),
    ("response", (3, {
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 1,
            "outputTokens": 25,
            "totalTokens": 8144,
            "cachedReadTokens": 7989,
            "cachedWriteTokens": 129,
        },
        "_meta": {},
    })),
]


def _fixture_lines(work_dir: str, extra_updates: list[dict] | None = None) -> list[bytes]:
    """Render ``ACP_1_18_34_FIXTURE_LINES`` with ``work_dir`` substituted.

    ``extra_updates`` are inserted as ``session/update`` notifications just
    before the final prompt response.
    """
    lines: list[bytes] = []
    for kind, payload in ACP_1_18_34_FIXTURE_LINES:
        if kind == "response":
            msg_id, result = payload
            if msg_id == 3 and extra_updates:
                for update in extra_updates:
                    lines.append(_make_acp_notification(
                        "session/update", {"sessionId": "ses_fixture", "update": update},
                    ))
            lines.append(_make_acp_response(msg_id, result))
        else:
            rendered = json.loads(json.dumps(payload).replace(_WORK_DIR, work_dir))
            lines.append(_make_acp_notification(
                "session/update", {"sessionId": "ses_fixture", "update": rendered},
            ))
    return lines


async def _run_fixture(tmp_path, extra_updates=None):
    proc = _mock_proc(_fixture_lines(str(tmp_path), extra_updates))
    with patch("container.tools.run_opencode_acp.asyncio.create_subprocess_exec",
               return_value=proc):
        return await run_opencode_acp(
            work_dir=str(tmp_path),
            task_description="Create a file TEST-1.md containing one line",
            timeout_seconds=60,
        )


class TestAcp11834Payloads:
    """The parser understands the real 1.18.34 notification shapes."""

    _LOGGER = "container.tools.run_opencode_acp"

    @pytest.mark.asyncio
    async def test_files_edited_relative_and_deduped(self, tmp_path):
        result = await _run_fixture(tmp_path)
        assert result["stop_reason"] == "end_turn"
        # ``locations[].path`` and ``rawInput.filePath`` both name the same
        # file across two updates; it is reported once, relative to work_dir.
        assert result["files_edited"] == ["TEST-1.md"]

    @pytest.mark.asyncio
    async def test_read_kind_contributes_nothing(self, tmp_path):
        read_update = {
            "sessionUpdate": "tool_call",
            "toolCallId": "tooluse_read1",
            "title": "read",
            "kind": "read",
            "status": "completed",
            "locations": [{"path": f"{tmp_path}/README.md"}],
            "rawInput": {"filePath": f"{tmp_path}/README.md"},
        }
        result = await _run_fixture(tmp_path, extra_updates=[read_update])
        assert result["files_edited"] == ["TEST-1.md"]

    @pytest.mark.asyncio
    async def test_edit_kind_remembered_across_updates(self, tmp_path):
        # kind only on the first update; the later update with locations
        # has no kind but must still be attributed to the edit tool call.
        updates = [
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tooluse_edit2",
                "title": "edit",
                "kind": "edit",
                "status": "pending",
                "locations": [],
                "rawInput": {},
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tooluse_edit2",
                "status": "completed",
                "title": "src/app.py",
                "locations": [{"path": f"{tmp_path}/src/app.py"}],
            },
        ]
        result = await _run_fixture(tmp_path, extra_updates=updates)
        assert result["files_edited"] == ["TEST-1.md", "src/app.py"]

    @pytest.mark.asyncio
    async def test_prompt_usage_reported(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger=self._LOGGER):
            result = await _run_fixture(tmp_path)

        assert result["usage"] == {
            "input_tokens": 1,
            "output_tokens": 25,
            "total_tokens": 8144,
            "cached_read_tokens": 7989,
            "cached_write_tokens": 129,
            "prompt_tokens": 8119,
        }
        completed = [r.getMessage() for r in caplog.records
                     if "prompt completed" in r.getMessage()]
        assert len(completed) == 1
        assert "prompt_tokens=8119" in completed[0]
        assert "uncached_input=1" in completed[0]
        assert "cached_read=7989" in completed[0]
        assert "cached_write=129" in completed[0]
        assert "output_tokens=25" in completed[0]
        # Non-zero total_tokens: no "0 tokens" warning.
        assert not any("0 tokens" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_usage_update_and_commands_not_in_stdout(self, tmp_path):
        result = await _run_fixture(tmp_path)
        assert result["stdout"] == "Created\n TEST-1.md\n."
        assert "8119" not in result["stdout"]
        assert "usage_update" not in result["stdout"]
        assert "available_commands_update" not in result["stdout"]
        assert "customize-opencode" not in result["stdout"]


class TestCollectEditedPaths:
    def test_kind_tracked_per_tool_call_id(self):
        kinds: dict[str, str] = {}
        first = {"toolCallId": "a", "kind": "edit", "locations": []}
        assert _collect_edited_paths(first, kinds, "/w") == []
        assert kinds == {"a": "edit"}
        later = {"toolCallId": "a", "locations": [{"path": "/w/x.py"}]}
        assert _collect_edited_paths(later, kinds, "/w") == ["x.py"]

    def test_unknown_tool_call_id_without_kind_ignored(self):
        update = {"toolCallId": "zzz", "locations": [{"path": "/w/x.py"}]}
        assert _collect_edited_paths(update, {}, "/w") == []

    def test_execute_kind_ignored(self):
        update = {"toolCallId": "b", "kind": "execute",
                  "locations": [{"path": "/w/x.py"}],
                  "rawInput": {"filePath": "/w/x.py"}}
        assert _collect_edited_paths(update, {}, "/w") == []

    def test_path_preferred_over_uri_and_raw_input_merged(self):
        update = {
            "toolCallId": "c", "kind": "edit",
            "locations": [
                {"path": "/w/a.py", "uri": "file:///w/ignored.py"},
                {"uri": "file:///w/b.py"},
                "not-a-dict",
                {"path": 5},
            ],
            "rawInput": {"filePath": "/w/a.py"},
        }
        assert _collect_edited_paths(update, {}, "/w") == ["a.py", "b.py"]

    def test_raw_input_non_string_file_path_ignored(self):
        update = {"toolCallId": "d", "kind": "edit", "rawInput": {"filePath": None}}
        assert _collect_edited_paths(update, {}, "/w") == []


class TestRelativize:
    def test_inside_work_dir(self):
        assert _relativize("/w/job/src/a.py", "/w/job") == "src/a.py"

    def test_outside_work_dir_unchanged(self):
        assert _relativize("/etc/hosts", "/w/job") == "/etc/hosts"

    def test_sibling_prefix_not_treated_as_inside(self):
        assert _relativize("/w/job-other/a.py", "/w/job") == "/w/job-other/a.py"

    def test_file_uri_prefix_stripped(self):
        assert _relativize("file:///w/job/a.py", "/w/job") == "a.py"

    def test_already_relative_unchanged(self):
        assert _relativize("a.py", "/w/job") == "a.py"
        assert _relativize("src/a.py", "/w/job") == "src/a.py"

    def test_work_dir_itself_unchanged(self):
        assert _relativize("/w/job", "/w/job") == "/w/job"


class TestParsePromptUsage:
    def test_full_1_18_34_block(self):
        usage = _parse_prompt_usage({
            "inputTokens": 1, "outputTokens": 25, "totalTokens": 8144,
            "cachedReadTokens": 7989, "cachedWriteTokens": 129,
        })
        assert usage["prompt_tokens"] == 8119
        assert usage["total_tokens"] == 8144

    @pytest.mark.parametrize("raw", [None, {}, "usage", [], {"inputTokens": "x"}])
    def test_missing_or_malformed_defaults_to_zero(self, raw):
        usage = _parse_prompt_usage(raw)
        assert set(usage) == {
            "input_tokens", "output_tokens", "total_tokens",
            "cached_read_tokens", "cached_write_tokens", "prompt_tokens",
        }
        assert all(v == 0 for v in usage.values())

    def test_coerces_numeric_strings_and_floats(self):
        usage = _parse_prompt_usage({"inputTokens": "10", "cachedReadTokens": 5.0})
        assert usage["prompt_tokens"] == 15
