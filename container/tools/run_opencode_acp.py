# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run OpenCode via ACP protocol over stdin/stdout.

Design notes for running OpenCode inside the AgentCore microVM:

* OpenCode is distributed as a Bun-compiled binary. Bun extracts its virtual
  filesystem (``bunfs``) to ``/tmp`` on first run; any read-only or
  PRoot-like isolation breaks it (GitHub issues #7960, #7843). The microVM
  writable ``/tmp`` works. The binary is the pinned GitHub release asset,
  installed at ``/usr/local/bin/opencode`` (see ``container/Dockerfile``).
* All OpenCode startup side effects (autoupdate check, LSP download,
  models.dev fetch, data-dir prune, default plugin load) hang or fail in
  the microVM. Disable them all via ``OPENCODE_DISABLE_*`` env vars
  (plumbed at runtime env + per-spawn).
* Config is passed inline via ``OPENCODE_CONFIG_CONTENT`` rather than a
  written config file. Whether it overrides a repo's own ``opencode.json``
  is not yet verified on the pinned release.
* We do **not** pre-drain stderr before sending ACP frames. ``opencode acp``
  is ready for stdin as soon as it starts; waiting on stderr to see a
  "migration complete" line is brittle and sometimes deadlocks because the
  binary interleaves stderr and the ACP reply stream.
"""

import asyncio
import json
import logging
import os
import signal
import time
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)
OPENCODE_BINARY = os.environ.get("OPENCODE_BINARY", "/usr/local/bin/opencode")


def _validate_opencode_binary(path: str) -> None:
    """Fail fast at server startup if ``OPENCODE_BINARY`` is unusable.

    Called once from ``container/code_mcp_server.py`` before the
    FastMCP server starts listening; not called per-invocation, so
    unit tests of ``run_opencode_acp`` that mock
    ``asyncio.create_subprocess_exec`` are unaffected.

    The binary path is deployment-time config (read once at import),
    not user input, so this is defence in depth rather than sandbox
    boundary enforcement. We check:

    * The value is a non-empty string.
    * The path is absolute. ``subprocess.create_subprocess_exec`` with
      a relative name would resolve via ``$PATH``, which is noisy
      inside the microVM and makes it harder to reason about which
      binary actually ran.
    * The path exists and is an executable regular file.

    Raised as ``RuntimeError`` so the startup path surfaces the
    misconfiguration with a clear message rather than a generic
    ``FileNotFoundError`` from deep inside ``create_subprocess_exec``
    on the first incoming request.
    """
    if not isinstance(path, str) or not path:
        raise RuntimeError("OPENCODE_BINARY must be a non-empty string")
    if not os.path.isabs(path):
        raise RuntimeError(
            f"OPENCODE_BINARY must be an absolute path; got {path!r}"
        )
    if not os.path.isfile(path):
        raise RuntimeError(
            f"OPENCODE_BINARY does not exist or is not a regular file: {path!r}"
        )
    if not os.access(path, os.X_OK):
        raise RuntimeError(
            f"OPENCODE_BINARY is not executable: {path!r}"
        )


class OpenCodeResult(TypedDict):
    stdout: str
    stderr: str
    stop_reason: str          # from PromptResponse.stopReason: "end_turn", "max_tokens", "max_requests", "refused", "cancelled"
    files_edited: list[str]   # paths (relative to work_dir) from edit-kind tool_call/tool_call_update notifications
    plan: list[dict]          # from plan notifications: [{"content": "...", "status": "..."}]
    usage: dict               # token usage from PromptResponse.usage, see _parse_prompt_usage


def _parse_prompt_usage(usage: object) -> dict:
    """Normalise the ``usage`` block of the ``session/prompt`` response.

    OpenCode 1.18.x reports ``inputTokens`` *excluding* prompt-cache hits,
    so a cached prompt shows ``inputTokens=1`` next to
    ``cachedReadTokens=7989``. ``prompt_tokens`` adds the three input-side
    counters back together so logs reflect the real prompt size. All
    values are coerced to ``int`` and default to 0.
    """
    usage = usage if isinstance(usage, dict) else {}

    def _int(key: str) -> int:
        value = usage.get(key, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _int("inputTokens")
    cached_read = _int("cachedReadTokens")
    cached_write = _int("cachedWriteTokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": _int("outputTokens"),
        "total_tokens": _int("totalTokens"),
        "cached_read_tokens": cached_read,
        "cached_write_tokens": cached_write,
        "prompt_tokens": input_tokens + cached_read + cached_write,
    }


def _relativize(path: str, work_dir: str) -> str:
    """Return ``path`` relative to ``work_dir`` when it lives inside it.

    Strips a ``file://`` prefix first. Absolute paths outside ``work_dir``
    and already-relative paths are returned unchanged.
    """
    if path.startswith("file://"):
        path = path[len("file://"):]
    if not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, work_dir)
    except ValueError:
        # Different drive on Windows; cannot be inside work_dir.
        return path
    if rel == os.curdir or rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        return path
    return rel


def _collect_edited_paths(
    update: dict, tool_kinds: dict[str, str], work_dir: str,
) -> list[str]:
    """Extract edited file paths from a ``tool_call``/``tool_call_update``.

    OpenCode 1.18.34 spreads one tool invocation over several updates
    keyed by ``toolCallId``: the initial ``tool_call`` carries ``kind``
    (``edit``/``read``/``execute``) but empty ``locations``; the
    ``in_progress`` update carries ``locations: [{"path": "/abs/..."}]``
    and ``rawInput.filePath``, sometimes without ``kind``; the
    ``completed`` update has neither. ``tool_kinds`` remembers the kind
    per ``toolCallId`` so later updates can be attributed, and only
    ``edit`` tool calls contribute paths (reads are ignored).

    ``locations[].uri`` is still read for older OpenCode releases.
    Paths are relativised to ``work_dir`` and deduped preserving order.
    """
    tool_call_id = update.get("toolCallId")
    kind = update.get("kind")
    if tool_call_id is not None and isinstance(kind, str) and kind:
        tool_kinds[tool_call_id] = kind
    if tool_kinds.get(tool_call_id) != "edit":
        return []

    candidates: list[str] = []
    locations = update.get("locations")
    if isinstance(locations, list):
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            value = loc.get("path") or loc.get("uri")
            if isinstance(value, str) and value:
                candidates.append(value)
    raw_input = update.get("rawInput")
    if isinstance(raw_input, dict):
        file_path = raw_input.get("filePath")
        if isinstance(file_path, str) and file_path:
            candidates.append(file_path)

    paths: list[str] = []
    for candidate in candidates:
        rel = _relativize(candidate, work_dir)
        if rel not in paths:
            paths.append(rel)
    return paths


# ACP JSON-RPC message IDs
_INIT_ID = 1
_SESSION_NEW_ID = 2
_SESSION_PROMPT_ID = 3


def _make_jsonrpc(id: int, method: str, params: dict) -> str:
    """Build a JSON-RPC 2.0 request string (newline-delimited)."""
    msg = {"jsonrpc": "2.0", "id": id, "method": method, "params": params}
    return json.dumps(msg) + "\n"


def _build_opencode_config() -> dict:
    """Build the inline OpenCode config dict.

    OpenCode v1.14+ has strict config validation — only known keys are
    allowed. The ``amazon-bedrock`` provider and its global-prefixed
    cross-region inference profiles (including
    ``global.anthropic.claude-opus-4-6-v1``) are built in, so we do not
    redeclare them in ``provider.amazon-bedrock.models`` — that only
    muddles resolution. We simply set the ``model`` field to point at
    the prefixed ID. The provider reads AWS credentials from the
    environment (IAM role on AgentCore, via the AWS SDK's default
    credential provider chain).
    """
    model_id = os.environ.get("OPENCODE_MODEL", "global.anthropic.claude-opus-4-6-v1")
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"amazon-bedrock/{model_id}",
        "permission": {
            "edit": "allow",
            "bash": "allow",
        },
        "autoupdate": False,
        "disabled_providers": ["opencode"],
    }


async def _read_line(stdout: asyncio.StreamReader, timeout: float) -> Optional[str]:
    """Read a single line from stdout with timeout. Returns None on EOF."""
    try:
        line = await asyncio.wait_for(stdout.readline(), timeout=timeout)
        if not line:
            return None
        return line.decode("utf-8").strip()
    except asyncio.TimeoutError:
        raise


async def _send_message(stdin: asyncio.StreamWriter, message: str) -> None:
    """Send a JSON-RPC message over stdin."""
    try:
        stdin.write(message.encode("utf-8"))
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise RuntimeError(f"OpenCode stdin closed: {exc}") from exc


def _selected_model(session_result: object) -> str:
    """Return the model id from a ``session/new`` result, for logging.

    OpenCode 1.18.x reports it as the ``currentValue`` of the
    ``configOptions`` entry whose ``id`` is ``"model"`` (``_meta`` is empty).
    Older versions used ``_meta.opencode.modelId``, kept as a fallback.
    Returns ``"unknown"`` if neither is present.
    """
    if not isinstance(session_result, dict):
        return "unknown"
    options = session_result.get("configOptions")
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict) and opt.get("id") == "model":
                value = opt.get("currentValue")
                if isinstance(value, str) and value:
                    return value
    meta = session_result.get("_meta")
    opencode_meta = meta.get("opencode") if isinstance(meta, dict) else None
    if isinstance(opencode_meta, dict):
        value = opencode_meta.get("modelId")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _is_agent_request(msg: object) -> bool:
    """True if ``msg`` is a JSON-RPC request from the agent to us.

    JSON-RPC ids are scoped per direction, so an agent request can carry
    the same numeric id as one of our own requests. Anything with both
    ``id`` and ``method`` is a request, never a response.
    """
    return isinstance(msg, dict) and "id" in msg and "method" in msg


def _build_agent_request_response(msg: dict) -> dict:
    """Build the JSON-RPC response for an agent -> client ACP request.

    ``session/request_permission`` is answered per the ACP v1 schema
    (https://agentclientprotocol.com/protocol/schema#session-request-permission
    and https://agentclientprotocol.com/protocol/tool-calls#requesting-permission):
    the result is ``{"outcome": {"outcome": "selected", "optionId": ...}}``
    or ``{"outcome": {"outcome": "cancelled"}}``. The headless pipeline has
    no user to ask, and the OpenCode config already allows ``edit`` and
    ``bash``, so any remaining permission request is rejected: pick the
    ``reject_once`` option when offered, then ``reject_always``, otherwise
    return ``cancelled``.

    Every other method (``fs/*``, ``terminal/*``, ...) is unsupported —
    we advertise no client capabilities — and gets JSON-RPC
    ``-32601 Method not found`` so the agent does not block waiting.
    """
    req_id = msg.get("id")
    method = msg.get("method")

    if method == "session/request_permission":
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        options = params.get("options")
        options = options if isinstance(options, list) else []
        tool_call = params.get("toolCall")
        title = tool_call.get("title", "") if isinstance(tool_call, dict) else ""

        # Prefer reject_once, then reject_always. "cancelled" is the last
        # resort because ACP reserves it for turns cancelled via
        # session/cancel, so the agent may treat it as aborting the turn.
        reject_option_id = None
        for kind in ("reject_once", "reject_always"):
            reject_option_id = next(
                (
                    opt.get("optionId")
                    for opt in options
                    if isinstance(opt, dict)
                    and opt.get("kind") == kind
                    and opt.get("optionId")
                ),
                None,
            )
            if reject_option_id is not None:
                break
        if reject_option_id is not None:
            outcome = {"outcome": "selected", "optionId": reject_option_id}
        else:
            outcome = {"outcome": "cancelled"}

        logger.info(
            "Rejecting ACP agent request: method=%s outcome=%s",
            method, outcome["outcome"],
        )
        # The tool title is often the full shell command; keep it out of
        # INFO-level CloudWatch logs.
        logger.debug("Rejected ACP permission request tool_title=%s", str(title)[:200])
        return {"jsonrpc": "2.0", "id": req_id, "result": {"outcome": outcome}}

    logger.info("Unsupported ACP agent request: method=%s", method)
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "Method not found"},
    }


async def _respond_to_agent_request(stdin: asyncio.StreamWriter, msg: dict) -> None:
    """Answer an agent -> client request so the agent does not block."""
    response = _build_agent_request_response(msg)
    await _send_message(stdin, json.dumps(response) + "\n")


async def _read_response_line(
    stdin: asyncio.StreamWriter,
    stdout: asyncio.StreamReader,
    timeout: float,
) -> Optional[str]:
    """Read the next line that is not an agent request or notification.

    Used for the ``initialize`` and ``session/new`` handshake. Lines that
    carry a ``method`` key are not responses: agent requests are answered
    and notifications are skipped. Any other line (including unparseable
    output) is returned unchanged so callers keep their existing parsing
    and error handling. Returns ``None`` on EOF.
    """
    deadline = time.monotonic() + timeout
    while True:
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            raise asyncio.TimeoutError("Timed out waiting for ACP response")
        line = await _read_line(stdout, timeout=time_left)
        if line is None:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(msg, dict) or "method" not in msg:
            return line
        if "id" in msg:
            await _respond_to_agent_request(stdin, msg)
        else:
            logger.debug("Skipping ACP notification during handshake: %s",
                         msg.get("method"))


async def _drain_stderr(proc: asyncio.subprocess.Process, buffer: list[str]) -> None:
    """Continuously read stderr into ``buffer`` so the pipe never fills up.

    A full stderr pipe will eventually block the child. This coroutine
    runs for the lifetime of the process and accumulates lines for
    post-mortem diagnostics.
    """
    if proc.stderr is None:
        return
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            decoded = line.decode("utf-8", errors="replace").rstrip()
            buffer.append(decoded)
            # Keep buffer bounded
            if len(buffer) > 500:
                del buffer[:-250]
            logger.info("OpenCode stderr: %s", decoded)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("stderr drain error: %s", exc)


_TERM_GRACE_SECONDS = 5.0


def _signal_group(pgid: int, sig: int) -> bool:
    """Send ``sig`` to process group ``pgid``.

    Returns True if delivered, False if no process is in the group or the
    signal could not be sent (logged at warning).
    """
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:  # includes PermissionError
        logger.warning("Could not signal OpenCode process group %s: %s", pgid, exc)
        return False


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate OpenCode and its whole process group.

    OpenCode is spawned with ``start_new_session=True``, so it leads its own
    process group (pgid == pid) and every process its bash tool starts is in
    that group unless it creates a new session itself. SIGTERM goes to the
    group, then SIGKILL after a 5s grace if the leader has not exited. The
    group is SIGKILLed once more afterwards even if the leader already
    exited, so a background child does not outlive the run.

    A descendant that deliberately starts a new session (setsid) leaves the
    group and is not covered; that is an accepted residual risk, since every
    process in the microVM already runs with the execution role. Safe to
    call more than once.
    """
    pgid = proc.pid
    # Never signal pgid 0/1 (our own group / init) if pid is unexpected.
    if not isinstance(pgid, int) or pgid <= 1:
        logger.warning("Unexpected OpenCode pid %r; not signalling a process group", pgid)
        return

    if proc.returncode is None:
        _signal_group(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
        except asyncio.TimeoutError:
            _signal_group(pgid, signal.SIGKILL)
            try:
                await proc.wait()
            except Exception:
                pass

    # Leader is gone; kill anything left in its group.
    _signal_group(pgid, signal.SIGKILL)


def _resolve_aws_credentials_into_env() -> dict:
    """Resolve AWS IAM-role credentials via boto3 and return them as env vars.

    The AgentCore microVM vends IAM-role credentials exclusively via
    IMDSv2 (at ``169.254.169.254``, role name ``execution_role``).
    Python boto3's default provider chain finds them fine.

    OpenCode's ``amazon-bedrock`` provider, however, short-circuits to
    ``autoload: false`` if NONE of these env sources are set:

    * ``AWS_PROFILE``
    * ``AWS_ACCESS_KEY_ID``
    * ``AWS_BEARER_TOKEN_BEDROCK``
    * ``AWS_WEB_IDENTITY_TOKEN_FILE``
    * ``AWS_CONTAINER_CREDENTIALS_{RELATIVE,FULL}_URI``

    IMDS is **not** in that gate (confirmed in upstream
    ``packages/opencode/src/provider/provider.ts``). The gate runs
    before ``fromNodeProviderChain()`` is ever called, so even though
    the Node SDK's default chain would pick up IMDS, the provider is
    never loaded and Bedrock calls silently return ``end_turn`` with
    zero tokens.

    Workaround: resolve the IAM role snapshot via boto3 and export it
    as classic env vars. Creds are valid for ~6 hours; coding sessions
    run for minutes; each subprocess spawn re-resolves fresh creds.
    """
    try:
        import boto3
        session = boto3.Session()
        creds = session.get_credentials()
        if creds is None:
            return {}
        frozen = creds.get_frozen_credentials()
        out = {
            "AWS_ACCESS_KEY_ID": frozen.access_key,
            "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
        }
        if frozen.token:
            out["AWS_SESSION_TOKEN"] = frozen.token
        return out
    except Exception as exc:
        logger.warning("Failed to resolve AWS credentials for OpenCode: %s", exc)
        return {}


def _build_spawn_env(work_dir: str) -> dict:
    """Env vars for the OpenCode subprocess.

    Scoped to what is proven needed to get OpenCode running headlessly.
    """
    # Resolve IAM-role creds via boto3 so we can pass them as classic env
    # vars. OpenCode's amazon-bedrock provider short-circuits to
    # ``autoload: false`` if AWS_ACCESS_KEY_ID (and a few other env
    # sources) are not set — IMDS alone does not satisfy its gate. See
    # the ``_resolve_aws_credentials_into_env`` docstring.
    aws_creds = _resolve_aws_credentials_into_env()
    env = {
        **os.environ,
        **aws_creds,
        # Autoupdate would try to download a new OpenCode binary on every
        # microVM cold start (new fs each session).
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        # Pass the config inline so OpenCode never reads a file from the
        # working tree (an on-disk opencode.json would otherwise be
        # committed into the PR). We deliberately do NOT set
        # OPENCODE_CONFIG (a path), because an on-disk project config
        # would override a path-based config.
        # NOTE: OPENCODE_CONFIG_CONTENT precedence over an on-disk project
        # opencode.json is to be verified against the pinned OpenCode
        # 1.18.34 binary (see container/Dockerfile ARG OPENCODE_VERSION)
        # during the end-to-end deploy.
        "OPENCODE_CONFIG_CONTENT": json.dumps(_build_opencode_config()),
    }
    # Drop any inherited OPENCODE_CONFIG path so a pre-set path-based
    # config source cannot reach the child alongside the inline content.
    # OpenCode treats the path and the inline content as separate config
    # sources, so copying os.environ above could otherwise smuggle a path
    # in. Remove it explicitly to honour the no-path requirement.
    env.pop("OPENCODE_CONFIG", None)
    return env


async def run_opencode_acp(
    work_dir: str,
    task_description: str,
    timeout_seconds: int,
) -> OpenCodeResult:
    """Spawn OpenCode as a subprocess and drive it over ACP (stdin/stdout).

    Sends ACP initialize -> session/new -> session/prompt, parses
    session/update notifications, and extracts stop_reason and
    files_edited from the final ACP response. Handles timeout with
    SIGTERM -> SIGKILL escalation on the process group.
    """
    collected_stdout: list[str] = []
    stderr_buffer: list[str] = []
    files_edited: list[str] = []
    tool_kinds: dict[str, str] = {}   # toolCallId -> kind ("edit"/"read"/"execute")
    plan_entries: list[dict] = []
    context_usage: dict = {}          # last session/update of kind usage_update
    prompt_usage: dict = _parse_prompt_usage({})
    stop_reason = "end_turn"
    spawn_env = _build_spawn_env(work_dir)

    logger.info(
        "Spawning OpenCode: binary=%s cwd=%s model=%s",
        OPENCODE_BINARY, work_dir, spawn_env.get("OPENCODE_MODEL"),
    )

    proc = await asyncio.create_subprocess_exec(
        OPENCODE_BINARY, "acp", "--log-level", "INFO",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        env=spawn_env,
        # Own session and process group, so _terminate_process can kill
        # every process OpenCode's tools started, not just OpenCode itself.
        start_new_session=True,
    )

    # Drain stderr in the background so the pipe never fills up.
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_buffer))

    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        remaining = float(timeout_seconds)

        # Step 1: Send initialize (no pre-drain of stderr — ACP accepts
        # stdin as soon as the process starts).
        await _send_message(
            proc.stdin,
            _make_jsonrpc(_INIT_ID, "initialize", {
                "protocolVersion": 1,
                "capabilities": {},
            }),
        )

        init_response = await _read_response_line(
            proc.stdin, proc.stdout, timeout=remaining,
        )
        if init_response is None:
            await asyncio.sleep(0.2)  # let stderr drain catch up
            stderr_snapshot = "\n".join(stderr_buffer[-30:])
            raise RuntimeError(
                f"OpenCode closed stdout before initialize response. "
                f"stderr tail: {stderr_snapshot[:1500]}"
            )

        try:
            init_parsed = json.loads(init_response)
            agent_info = init_parsed.get("result", {}).get("agentInfo", {})
            logger.info(
                "OpenCode ACP initialized: version=%s",
                agent_info.get("version", "?"),
            )
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Could not parse init response: %s", init_response[:200])

        # Step 2: Send session/new
        await _send_message(
            proc.stdin,
            _make_jsonrpc(_SESSION_NEW_ID, "session/new", {
                "cwd": work_dir,
                "mcpServers": [],
            }),
        )

        session_response_line = await _read_response_line(
            proc.stdin, proc.stdout, timeout=remaining,
        )
        if session_response_line is None:
            raise RuntimeError("OpenCode closed stdout before session/new response")

        session_response = json.loads(session_response_line)
        session_id = session_response.get("result", {}).get("sessionId", "")
        if not session_id:
            raise RuntimeError(
                f"No sessionId in session/new response: {session_response_line}"
            )

        selected_model = _selected_model(session_response.get("result", {}))
        logger.info(
            "OpenCode ACP session created: session_id=%s, model=%s",
            session_id, selected_model,
        )

        # Step 3: Send session/prompt
        logger.info("Sending session/prompt (task len=%d)", len(task_description))
        try:
            await _send_message(
                proc.stdin,
                _make_jsonrpc(_SESSION_PROMPT_ID, "session/prompt", {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": task_description}],
                }),
            )
            logger.info("session/prompt sent successfully")
        except Exception as exc:
            logger.error("Failed to send session/prompt: %s", exc)
            raise

        # Step 4: Read stdout, parsing responses and notifications
        deadline = time.monotonic() + remaining
        iteration = 0

        while True:
            iteration += 1
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                raise asyncio.TimeoutError("OpenCode execution timed out")

            try:
                line = await _read_line(proc.stdout, timeout=time_left)
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                logger.error("read_line raised on iter %d: %s", iteration, exc)
                raise

            if line is None:
                # EOF — process exited without sending the final response.
                await asyncio.sleep(0.2)  # let stderr drain catch up
                logger.warning(
                    "OpenCode stdout EOF at iter=%d before prompt response. "
                    "stderr tail: %s",
                    iteration,
                    "\n".join(stderr_buffer[-30:])[:1500],
                )
                break

            if not line:
                continue

            logger.debug("Received line (iter=%d, len=%d): %s",
                         iteration, len(line), line[:300])

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                collected_stdout.append(line)
                continue

            # Notification (no "id"): progress / tool updates / plan
            if "method" in msg and "id" not in msg:
                method = msg["method"]
                params = msg.get("params", {})

                if method == "session/update":
                    update = params.get("update", {})
                    update_type = update.get("sessionUpdate", "")

                    if update_type == "agent_message_chunk":
                        content = update.get("content", {})
                        text = content.get("text", "")
                        if text:
                            collected_stdout.append(text)

                    elif update_type in ("tool_call", "tool_call_update"):
                        for p in _collect_edited_paths(update, tool_kinds, work_dir):
                            if p not in files_edited:
                                files_edited.append(p)

                    elif update_type == "plan":
                        plan_entries.clear()
                        plan_entries.extend(update.get("entries", []))

                    elif update_type == "usage_update":
                        # Context-window accounting ({used, size, cost});
                        # informational only, never part of stdout.
                        context_usage = {
                            k: update.get(k) for k in ("used", "size", "cost")
                            if k in update
                        }
                        logger.debug("OpenCode ACP usage_update: %s", context_usage)

                    else:
                        # available_commands_update and other unknown kinds
                        # only reach stdout when they carry a message.
                        update_msg = params.get("message", "") or update.get("message", "")
                        if update_msg:
                            collected_stdout.append(update_msg)
                else:
                    collected_stdout.append(line)
                continue

            # Request from the agent to us (has both "id" and "method"),
            # e.g. session/request_permission. Answer it, otherwise the
            # agent blocks until the task timeout.
            if _is_agent_request(msg):
                await _respond_to_agent_request(proc.stdin, msg)
                continue

            # Response to session/prompt: id == 3 and no "method" key.
            # Ids are per-direction, so an agent request may reuse id 3;
            # those are handled above and never end the loop.
            if msg.get("id") == _SESSION_PROMPT_ID and "method" not in msg:
                result = msg.get("result", {})
                stop_reason = result.get("stopReason", "end_turn")
                prompt_usage = _parse_prompt_usage(result.get("usage"))
                total_tokens = prompt_usage["total_tokens"]
                logger.info(
                    "OpenCode ACP prompt completed: stop_reason=%s "
                    "total_tokens=%s prompt_tokens=%s (uncached_input=%s "
                    "cached_read=%s cached_write=%s) output_tokens=%s",
                    stop_reason,
                    total_tokens,
                    prompt_usage["prompt_tokens"],
                    prompt_usage["input_tokens"],
                    prompt_usage["cached_read_tokens"],
                    prompt_usage["cached_write_tokens"],
                    prompt_usage["output_tokens"],
                )
                if "error" in msg:
                    raise RuntimeError(
                        f"OpenCode ACP error: {msg['error'].get('message', 'Unknown')}"
                    )
                if total_tokens == 0 and stop_reason == "end_turn":
                    # No model call happened — warn with context so we can debug.
                    logger.warning(
                        "OpenCode returned end_turn with 0 tokens — no LLM "
                        "call was made. Most likely cause: AWS creds not "
                        "reaching OpenCode's aws-sdk-js."
                    )
                break

            collected_stdout.append(line)

    except asyncio.TimeoutError:
        await _terminate_process(proc)
        stderr_task.cancel()
        try:
            await asyncio.wait_for(stderr_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        raise RuntimeError(
            f"OpenCode timed out after {timeout_seconds}s. "
            f"stderr tail: {chr(10).join(stderr_buffer[-30:])[:1000]}"
        )
    except Exception as exc:
        logger.exception("Unexpected error in OpenCode ACP loop: %s", exc)
        await _terminate_process(proc)
        raise
    finally:
        # Always run, even if OpenCode already exited, so background
        # processes in its group are killed before the caller continues.
        try:
            await _terminate_process(proc)
        finally:
            stderr_task.cancel()
            try:
                await asyncio.wait_for(stderr_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    collected_stderr = "\n".join(stderr_buffer)

    # A negative returncode means we terminated via signal (SIGTERM=-15,
    # SIGKILL=-9). When we've already broken out of the read loop with
    # a successful stop_reason, the SIGTERM we sent in ``finally`` is
    # expected and not a failure. Only raise on positive non-zero codes,
    # which indicate the binary itself exited with an error.
    if proc.returncode and proc.returncode > 0:
        # The actual error is usually at the end of stderr, so report the
        # tail rather than startup output.
        logger.error(
            "OpenCode exited with code %d. stderr tail: %s",
            proc.returncode, collected_stderr[-1500:],
        )
        raise RuntimeError(
            f"OpenCode exited with code {proc.returncode}. "
            f"stderr tail: {collected_stderr[-500:]}"
        )

    return OpenCodeResult(
        stdout="\n".join(collected_stdout),
        stderr=collected_stderr,
        stop_reason=stop_reason,
        files_edited=files_edited,
        plan=plan_entries,
        usage=prompt_usage,
    )

