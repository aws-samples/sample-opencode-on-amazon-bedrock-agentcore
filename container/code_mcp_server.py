# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OpenCode MCP Server — single FastMCP server on port 8000.

Exposes 6 OpenCode tools via Streamable HTTP:
  - code             (sync — relays progress notifications when the client sends a
                      progressToken, supports ctx.elicit() for OAuth)
  - run_coding_task   (async — returns job_id immediately, runs pipeline in background)
  - connect_git_host  (interactive — OAuth consent flow via ctx.elicit())
  - get_task_status   (query — read job record from DynamoDB)
  - list_tasks        (query — list user's jobs from DynamoDB)
  - cancel_task       (control — StopRuntimeSession on the job's recorded session;
                      in-process only when the job runs on the same microVM)

Requirements: 1.1-1.6, 2.1-2.7, 3.1-3.4, 4.1-4.6, 5.1-5.3,
              6.1-6.4, 8.1-8.5, 15.1, 15.2, 16.1,
              17.1-17.4, 22.1-22.4
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Mapping

import boto3
from botocore.exceptions import ClientError

# Configure structured JSON logging to stdout so CloudWatch Logs Insights
# can filter on specific fields like job_id, user_id, and status.
from pythonjsonlogger import json as jsonlogger

_handler = logging.StreamHandler(sys.stdout)
_formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
)
_handler.setFormatter(_formatter)
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)

from fastmcp import Context, FastMCP
from bedrock_agentcore.runtime import BedrockAgentCoreApp

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:  # pragma: no cover - only hit when fastmcp is stubbed

    def get_http_headers(include_all: bool = False, include=None) -> dict:
        """Fallback used when FastMCP's dependency helper is unavailable."""
        return {}

from container.lib.dynamodb_helpers import (
    JobStateConflict,
    query_job_record,
    query_user_jobs,
    serialize_job_record,
    update_job_status,
)
from container.pipeline import run_coding_pipeline
from container.lib.credential_errors import GIT_HOST_NOT_CONNECTED_MESSAGE
from container.tools import resolve_git_credential

# ---------------------------------------------------------------------------
# FastMCP + AgentCore app
# ---------------------------------------------------------------------------
mcp = FastMCP("opencode")
app = BedrockAgentCoreApp()


# /ping health check on port 8000 — required by the AgentCore platform.
# BedrockAgentCoreApp manages Healthy/HealthyBusy via add_async_task.
@mcp.custom_route("/ping", methods=["GET"])
async def ping(request):
    from starlette.responses import JSONResponse

    status = app.get_current_ping_status()
    return JSONResponse({"status": status.value})

# In-process task registry for cancellation signaling (Req 7.1)
_running_tasks: dict[str, asyncio.Task] = {}
_cancel_flags: dict[str, bool] = {}

# ── Environment variables for control tools ───────────────────────────────
ELICITATION_TIMEOUT_S = int(os.environ.get("ELICITATION_TIMEOUT_S", "300"))
# How long cancel_task waits for an in-process task to finish after
# task.cancel() before falling back to cross-session StopRuntimeSession.
IN_PROCESS_CANCEL_TIMEOUT_S = float(
    os.environ.get("IN_PROCESS_CANCEL_TIMEOUT_S", "10")
)
REGION = os.environ.get("AWS_REGION", "us-east-1")

# ── Runtime session id helpers ────────────────────────────────────────────

_RUNTIME_SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
_BAGGAGE_SESSION_KEY = "session.id"


def _runtime_session_id_from_headers(headers: Mapping[str, str]) -> str:
    """Extract the AgentCore runtime session id from inbound HTTP headers.

    Header names are matched case-insensitively. The dedicated
    ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` header is checked first
    for forward compatibility with the documented AgentCore contract. In
    practice (observed on the live platform, not a documented contract) the
    Gateway -> Runtime hop does not forward that header; the runtime
    session id reaches the container only as the ``session.id`` member of
    the W3C ``baggage`` header, e.g.
    ``Self=1-6ac63484-...,session.id=f67ddcd1-dc44-4867-8793-e4888e672b6b``.
    That value is what ``StopRuntimeSession`` accepts as ``runtimeSessionId``
    (the ``mcp-session-id`` header is the MCP transport session, not the
    runtime session, and is rejected by the API).

    Returns ``''`` when neither source carries a session id.
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}

    direct = (lowered.get(_RUNTIME_SESSION_HEADER) or "").strip()
    if direct:
        return direct

    baggage = lowered.get("baggage") or ""
    for member in baggage.split(","):
        member = member.strip()
        if not member or "=" not in member:
            continue
        key, _, rest = member.partition("=")
        if key.strip() != _BAGGAGE_SESSION_KEY:
            continue
        # Drop any ``;property`` suffix per the W3C Baggage grammar.
        value = rest.split(";", 1)[0].strip()
        if value:
            return value
    return ""


def _current_runtime_session_id() -> str:
    """Return the runtime session id for the request being handled, or ``''``.

    Reads the inbound headers via FastMCP's ``get_http_headers`` (which
    only works inside a request context) and delegates to
    :func:`_runtime_session_id_from_headers`. Never raises.
    """
    try:
        headers = get_http_headers(include_all=True) or {}
    except Exception:
        logger.warning("get_http_headers failed; no runtime session id", exc_info=True)
        headers = {}

    session_id = _runtime_session_id_from_headers(headers)
    if not session_id:
        logger.warning(
            "No AgentCore runtime session id in inbound headers (names=%s); "
            "cross-session cancel will be unavailable for this job",
            sorted(str(k).lower() for k in headers.keys()),
        )
    return session_id

# ── Elicitation timeout helper ─────────────────────────────────────────────

async def _elicit_with_timeout(ctx, *, message, schema):
    """Wrap ctx.elicit with the configured timeout.

    Returns None on timeout OR on any elicitation failure (e.g., FastMCP version
    mismatch raising TypeError, unsupported elicitation raising AttributeError,
    transport failures raising ConnectionError). Callers already handle None
    correctly (treated as cancellation / fallback to structured error).
    """
    try:
        return await asyncio.wait_for(
            ctx.elicit(message=message, schema=schema),
            timeout=ELICITATION_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Elicitation timed out after %ds", ELICITATION_TIMEOUT_S)
        return None
    except Exception:
        logger.warning(
            "_elicit_with_timeout: elicitation failed", exc_info=True
        )
        return None


# ── Credential helper ─────────────────────────────────────────────────────

def _get_credential(user_id: str, git_host: str):
    """Return (token, None) or (None, auth_url) for ``git_host``.

    Thin adaptor over the pipeline's ``resolve_git_credential`` so
    ``connect_git_host`` and the coding pipeline share one code path.
    """
    cred = resolve_git_credential(user_id=user_id, repo_url=f"https://{git_host}/")
    if cred.get("authorization_required"):
        return None, cred.get("auth_url", "")
    return cred["token"], None


# ── Response helpers ──────────────────────────────────────────────────────

def _ok(status: str, git_host: str, message: str):
    return {"status": status, "git_host": git_host, "message": message}


def _fail(git_host: str, error: str):
    return {"status": "failed", "git_host": git_host, "message": error, "error": error}


# Managed session storage base path (Req 16.1)
SESSION_STORAGE_PATH = os.environ.get(
    "SESSION_STORAGE_PATH", "/tmp/opencode-sessions"
)


_discovered_runtime_arn: str = ""


def _discover_runtime_arn_by_name(runtime_name: str) -> str:
    """Look up this runtime's ARN via ListAgentRuntimes (cached once found).

    CloudFormation cannot inject a resource's own ARN into its environment
    and the platform does not expose it inside the container, so the ARN is
    discovered from the control plane by name on first use.
    """
    global _discovered_runtime_arn
    if _discovered_runtime_arn:
        return _discovered_runtime_arn
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=REGION)
        paginator = client.get_paginator("list_agent_runtimes")
        for page in paginator.paginate():
            for rt in page.get("agentRuntimes", []):
                if rt.get("agentRuntimeName") == runtime_name:
                    _discovered_runtime_arn = rt.get("agentRuntimeArn", "")
                    return _discovered_runtime_arn
        logger.warning("No agent runtime named %r found in %s", runtime_name, REGION)
    except Exception as exc:  # noqa: BLE001 - never let discovery break the caller
        logger.warning("ListAgentRuntimes failed while resolving runtime ARN: %s", exc)
    return ""


def _get_runtime_arn() -> str:
    """Resolve the AgentCore runtime ARN.

    Checks RUNTIME_ARN first (direct), then constructs from
    RUNTIME_ARN_PREFIX + AGENT_RUNTIME_ID, then discovers the ARN by
    RUNTIME_NAME through the control plane. Returns '' if all fail.
    """
    arn = os.environ.get("RUNTIME_ARN") or os.environ.get("OPENCODE_RUNTIME_ARN", "")
    if arn:
        return arn
    prefix = os.environ.get("RUNTIME_ARN_PREFIX", "")
    runtime_id = os.environ.get("AGENT_RUNTIME_ID", "")
    if prefix and runtime_id:
        return f"{prefix}{runtime_id}"
    runtime_name = os.environ.get("RUNTIME_NAME", "")
    if runtime_name:
        return _discover_runtime_arn_by_name(runtime_name)
    return ""


# ---------------------------------------------------------------------------
# Helper: build a work directory under managed session storage
# ---------------------------------------------------------------------------
def _work_dir_for_job(job_id: str) -> str:
    """Return a work directory path under managed session storage."""
    path = os.path.join(SESSION_STORAGE_PATH, job_id)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Tool 1: code (sync) — Req 1.1, 2.1-2.7, 3.1-3.4
# ---------------------------------------------------------------------------
@mcp.tool()
async def code(
    task_description: str,
    repo_url: str,
    base_branch: str,
    target_branch: str = "",
    timeout_minutes: int = 10,
    _user_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Execute a coding task synchronously and return the result.

    Use this tool for quick, focused tasks (file creation, small edits,
    config changes) where you want the PR URL back immediately in the
    same conversation turn. The connection stays open for the full
    duration (typically 10-30 seconds). Progress notifications are relayed
    when the client sends a progressToken. If git credentials are missing,
    an OAuth consent prompt is shown inline.

    Prefer run_coding_task (async) instead when the task is complex
    (multi-file refactors, large features) and may take several minutes,
    or when you want to fire-and-forget and check status later.

    Behaviour: the pipeline always commits every change left in the
    working tree and opens a pull request against base_branch, regardless
    of task wording (asking the agent not to commit has no effect).
    base_branch must already exist on the remote. Changes are pushed to
    target_branch, which defaults to opencode/<job_id> and must differ
    from base_branch. files_edited lists paths relative to the repository
    root.
    """
    # --- Validation ---
    if not _user_id:
        return {"status": "failed", "error": "No user_id available"}
    if timeout_minutes < 1 or timeout_minutes > 30:
        return {
            "status": "failed",
            "error": "timeout_minutes must be between 1 and 30",
        }

    job_id = str(uuid.uuid4())
    branch = target_branch or f"opencode/{job_id}"
    work_dir = _work_dir_for_job(job_id)

    async def _on_progress(progress: int, total: int, message: str) -> None:
        await ctx.report_progress(progress=progress, total=total, message=message)

    async def _on_oauth_needed(auth_url: str) -> bool:
        elicit_result = await _elicit_with_timeout(
            ctx,
            message=(
                "Please authorize git access.\n\n"
                f"Open: {auth_url}\n\n"
                "Confirm when done."
            ),
            schema={
                "type": "object",
                "properties": {
                    "confirmation": {"type": "string", "default": "done"}
                },
            },
        )
        if elicit_result is None:
            # Timeout or elicitation exception — surface a user-friendly
            # credential-not-connected error rather than the terse
            # "OAuth authorization cancelled" message. The generic pipeline
            # handler stringifies this RuntimeError into the response's
            # `error` field unchanged. Per Property 1 in design.md, the
            # `error` field must equal GIT_HOST_NOT_CONNECTED_MESSAGE
            # exactly — the authorization URL is surfaced separately
            # through the `connect_git_host` tool's `action_required`
            # response, not by appending to this error string.
            raise RuntimeError(GIT_HOST_NOT_CONNECTED_MESSAGE)
        if getattr(elicit_result, "action", None) == "cancel":
            # Genuine user cancellation — preserve the existing
            # "OAuth authorization cancelled" pipeline path.
            return False
        return True

    return await run_coding_pipeline(
        user_id=_user_id,
        job_id=job_id,
        task_description=task_description,
        repo_url=repo_url,
        base_branch=base_branch,
        target_branch=branch,
        work_dir=work_dir,
        timeout_minutes=timeout_minutes,
        metric_prefix="code",
        runtime_session_id=_current_runtime_session_id(),
        on_progress=_on_progress,
        on_oauth_needed=_on_oauth_needed,
        cancel_flag=None,
    )


# ---------------------------------------------------------------------------
# Tool 2: run_coding_task (async) — Req 4.1-4.5, 5.1, 5.2
# ---------------------------------------------------------------------------
@mcp.tool()
async def run_coding_task(
    task_description: str,
    repo_url: str,
    base_branch: str,
    target_branch: str = "",
    timeout_minutes: int = 10,
    _user_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Submit a coding task for background execution. Returns a job_id immediately.

    Use this tool for complex or long-running tasks (multi-file refactors,
    large features, test suites) where you don't want to block the
    conversation. Poll with get_task_status to check progress. The task
    runs in the background and creates a PR when done.

    If git credentials are missing, the task fails immediately with
    'git_host_not_connected' -- call connect_git_host first.

    Prefer code (sync) instead for quick tasks where you want the PR
    URL back in the same turn.

    Behaviour: the pipeline always commits every change left in the
    working tree and opens a pull request against base_branch, regardless
    of task wording (asking the agent not to commit has no effect).
    base_branch must already exist on the remote. Changes are pushed to
    target_branch, which defaults to opencode/<job_id> and must differ
    from base_branch. files_edited lists paths relative to the repository
    root.
    """
    if not _user_id:
        return {"status": "failed", "error": "No user_id available"}
    if timeout_minutes < 1 or timeout_minutes > 30:
        return {
            "status": "failed",
            "error": "timeout_minutes must be between 1 and 30",
        }

    job_id = str(uuid.uuid4())
    branch = target_branch or f"opencode/{job_id}"
    work_dir = _work_dir_for_job(job_id)

    # Capture runtime_session_id from the inbound request (Req 4.4); the
    # pipeline persists it into the initial RUNNING DynamoDB row so
    # cancel_task can fall back to StopRuntimeSession.
    runtime_session_id = _current_runtime_session_id()

    # Register with AgentCore async task management (Req 4.2, 15.1). The
    # SDK returns an integer handle; complete_async_task must receive that
    # exact handle or the Runtime stays HealthyBusy forever.
    task_id = app.add_async_task(job_id)

    # Set up cancellation flag (Req 7.1)
    _cancel_flags[job_id] = False

    async def _background() -> None:
        try:
            await run_coding_pipeline(
                user_id=_user_id,
                job_id=job_id,
                task_description=task_description,
                repo_url=repo_url,
                base_branch=base_branch,
                target_branch=branch,
                work_dir=work_dir,
                timeout_minutes=timeout_minutes,
                metric_prefix="async_task",
                runtime_session_id=runtime_session_id,
                on_progress=None,
                on_oauth_needed=None,
                cancel_flag=lambda: _cancel_flags.get(job_id, False),
            )
        finally:
            try:
                completed = app.complete_async_task(task_id)
                logger.info(
                    "complete_async_task(%s) for job %s -> %s",
                    task_id, job_id, completed,
                )
            except Exception:
                logger.exception(
                    "Failed to complete_async_task for job %s", job_id
                )
            _running_tasks.pop(job_id, None)
            _cancel_flags.pop(job_id, None)

    _running_tasks[job_id] = asyncio.create_task(_background())

    # Return immediately (Req 4.3)
    return {"job_id": job_id, "status": "RUNNING"}


# ---------------------------------------------------------------------------
# Tool 3: connect_git_host (interactive) — Req 1.2
# ---------------------------------------------------------------------------
@mcp.tool()
async def connect_git_host(git_host: str, _user_id: str = "", ctx: Context | None = None) -> dict:
    """Connect a git host (GitHub) by completing OAuth authorization.

    Run this before submitting coding tasks to a new git host.
    """
    if not git_host:
        return _fail("", "git_host is required")

    user_id = _user_id
    if not user_id:
        return _fail(git_host, "No user_id available")

    # 1. Check existing credentials
    try:
        access_token, auth_url = _get_credential(user_id, git_host)
    except Exception as exc:
        err = str(exc)
        if "No credential provider" in err or "ResourceNotFoundException" in err:
            return _fail(git_host, f"No credential provider registered for '{git_host}'. Contact your administrator.")
        return _fail(git_host, f"Failed to check git host credentials: {err}")

    if access_token:
        return _ok("already_connected", git_host, f"Already connected to {git_host}.")

    # 2. Elicit — present auth URL to user
    if ctx is None:
        return _fail(git_host, "No MCP context available for elicitation")

    elicit_msg = (
        f"Please authorize git access for {git_host}.\n\n"
        f"Open this URL in your browser to authorize:\n{auth_url}\n\n"
        "After authorizing, return here and confirm."
    )

    try:
        result = await _elicit_with_timeout(
            ctx,
            message=elicit_msg,
            schema={
                "type": "object",
                "properties": {
                    "confirmation": {
                        "type": "string",
                        "description": "Type 'done' after completing authorization in your browser",
                        "default": "done",
                    }
                },
            },
        )
    except Exception:
        # Elicitation not supported or failed — fall back to returning URL
        return {
            "status": "action_required",
            "git_host": git_host,
            "message": (
                f"Please open this URL in your browser to authorize git access for {git_host}:\n\n"
                f"{auth_url}\n\n"
                "After authorizing, call connect_git_host again to verify the connection."
            ),
            "authorization_url": auth_url,
        }

    if result is None or getattr(result, "action", None) == "cancel":
        # User cancelled or client doesn't support elicitation — return URL directly
        return {
            "status": "action_required",
            "git_host": git_host,
            "message": (
                f"Please open this URL in your browser to authorize git access for {git_host}:\n\n"
                f"{auth_url}\n\n"
                "After authorizing, call connect_git_host again to verify the connection."
            ),
            "authorization_url": auth_url,
        }

    # 3. Verify token after user confirms
    for _attempt in range(2):
        try:
            access_token, _ = _get_credential(user_id, git_host)
            if access_token:
                return _ok("connected", git_host, f"Successfully connected to {git_host}.")
        except Exception:
            pass

    return _fail(
        git_host,
        "Authorization not detected. Please try again and ensure you complete the OAuth flow in your browser.",
    )


# ---------------------------------------------------------------------------
# Tool 4: get_task_status (query) — Req 1.3
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_task_status(job_id: str, _user_id: str = "") -> dict:
    """Get the status of a coding task by job_id.

    Queries DynamoDB scoped to the authenticated user. Returns the same
    record shape as each entry in list_tasks.
    """
    if not _user_id:
        return {"error": "No user_id available"}

    record = await query_job_record(job_id=job_id, user_id=_user_id)
    if not record:
        return {"error": "Job not found"}

    return serialize_job_record(record)


# ---------------------------------------------------------------------------
# Tool 5: list_tasks (query) — Req 1.4
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_tasks(
    status: str = "",
    limit: int = 50,
    _user_id: str = "",
) -> dict:
    """List coding tasks for the authenticated user.

    Optional status filter. Limit capped at 100. Each job has the same
    shape as a get_task_status response.
    """
    if not _user_id:
        return {"error": "No user_id available"}

    result = await query_user_jobs(
        user_id=_user_id,
        status_filter=status,
        limit=min(limit, 100),
    )
    return {
        "jobs": [serialize_job_record(j) for j in result["jobs"]],
        "count": result["count"],
    }


# ---------------------------------------------------------------------------
# Tool 6: cancel_task (control) — Req 1.5, 6.1, 6.2, 6.3
# ---------------------------------------------------------------------------
_TERMINAL_STATES = ("COMPLETE", "FAILED", "CANCELLED")


def _duration_since(created_at: str) -> float:
    """Seconds elapsed since an ISO-8601 ``created_at``; 0.0 if unparseable."""
    try:
        created = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - created).total_seconds(), 2)


def _cancel_failed(job_id: str, status: str, detail: str) -> dict:
    """Build the ``cancel_failed`` response shape (nothing was stopped)."""
    return {
        "job_id": job_id,
        "status": status,
        "error": "cancel_failed",
        "detail": detail,
    }


_HANDLER_RECORDED_DETAIL = (
    "CANCELLED was recorded by the job's own cancellation handler"
)


async def _record_cancelled(job_id: str, user_id: str, record: dict) -> str | None:
    """Conditionally write the CANCELLED row for a job that was stopped.

    Returns ``None`` when this call wrote the row. If the record already
    left ``RUNNING`` (another writer won the race) nothing is written and
    the record's current status is returned instead so the caller can
    decide: ``CANCELLED`` means the stopped microVM's pipeline recorded the
    cancellation itself during shutdown (the cancel still succeeded);
    ``COMPLETE`` / ``FAILED`` mean the job finished before the stop took
    effect.
    """
    try:
        await update_job_status(
            job_id=job_id,
            user_id=user_id,
            status="CANCELLED",
            expected_status="RUNNING",
            error="Task cancelled by user",
            pr_url="",
            stop_reason="",
            files_edited=[],
            duration_seconds=_duration_since(record.get("created_at", "")),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except JobStateConflict:
        latest = await query_job_record(job_id=job_id, user_id=user_id)
        status = (latest or record).get("status", "")
        logger.info(
            "cancel_task job %s: record already %s before CANCELLED was recorded",
            job_id, status,
        )
        return status
    return None


@mcp.tool()
async def cancel_task(job_id: str, _user_id: str = "") -> dict:
    """Cancel a running coding task and report whether it was actually stopped.

    Only the job's owner can cancel it. The task is stopped in-process when
    it runs on this microVM; otherwise the microVM recorded for the job is
    terminated via StopRuntimeSession.

    Response contract:

    - Stopped (or provably no longer running):
      {"job_id", "status": "CANCELLED",
       "method": "in_process" | "stop_runtime_session" | "session_already_terminated"}
      The job record is updated to CANCELLED. An optional "detail" string
      explains how the outcome was reached.
    - Nothing could be stopped:
      {"job_id", "status": <current record status, usually "RUNNING">,
       "error": "cancel_failed", "detail": <human-readable reason>}
      The job record is NOT modified; the task may still be running.
    - Unknown job: {"error": "Job not found"}
    - Already finished: {"error": "Job is already in terminal state: <STATUS>"}
    """
    if not _user_id:
        return {"error": "No user_id available"}

    # Query DynamoDB scoped to user
    record = await query_job_record(job_id=job_id, user_id=_user_id)
    if not record:
        return {"error": "Job not found"}

    # Reject terminal state jobs
    current_status = record.get("status", "")
    if current_status in _TERMINAL_STATES:
        return {"error": f"Job is already in terminal state: {current_status}"}

    session_id = record.get("runtime_session_id", "") or ""
    detail_prefix = ""

    # ── 1. In-process cancellation (job runs on this microVM) ─────────────
    task = _running_tasks.get(job_id)
    if task is not None:
        try:
            _cancel_flags[job_id] = True
            task.cancel()
            done, _pending = await asyncio.wait(
                {task}, timeout=IN_PROCESS_CANCEL_TIMEOUT_S
            )
        except Exception:
            logger.warning(
                "In-process cancellation raised for job %s; falling back to "
                "StopRuntimeSession", job_id, exc_info=True,
            )
            done = set()

        if done:
            # The pipeline's CancelledError handler wrote CANCELLED itself.
            latest = await query_job_record(job_id=job_id, user_id=_user_id)
            latest_status = (latest or {}).get("status", "")
            if latest_status == "CANCELLED":
                logger.info(
                    "cancel_task job %s: method=in_process runtime_session_id=%s",
                    job_id, session_id,
                )
                return {"job_id": job_id, "status": "CANCELLED", "method": "in_process"}
            if latest_status in _TERMINAL_STATES:
                # The task finished on its own before the cancel took effect.
                logger.info(
                    "cancel_task job %s: finished as %s before in-process cancel",
                    job_id, latest_status,
                )
                return {"error": f"Job is already in terminal state: {latest_status}"}
            detail_prefix = (
                "in-process task finished after cancel but the record is "
                f"still {latest_status or 'RUNNING'}; "
            )
        else:
            detail_prefix = (
                "in-process cancel signalled but the task did not finish "
                f"within {IN_PROCESS_CANCEL_TIMEOUT_S:g}s; "
            )

    # ── 2. Cross-session cancellation via StopRuntimeSession ──────────────
    # NOTE: when the in-process branch above fell through (task is not None),
    # ``session_id`` is THIS microVM's own runtime session, so the stop below
    # terminates the VM serving this very request: the caller sees a transport
    # error instead of a response and the CANCELLED row is then written by the
    # pipeline's own CancelledError handler during shutdown, not by this tool.
    # Accepted as a last resort for a task that ignores cancellation; through
    # the Gateway every call lands on a fresh microVM, so this path is not
    # reached in practice.
    if not session_id:
        detail = (
            detail_prefix
            + "job record has no runtime_session_id; cannot locate the "
            "microVM running it"
        )
        logger.info("cancel_task job %s: cancel_failed (%s)", job_id, detail)
        return _cancel_failed(job_id, current_status, detail)

    # May call the control plane on first use; keep it off the event loop.
    runtime_arn = await asyncio.to_thread(_get_runtime_arn)
    if not runtime_arn:
        logger.warning(
            "Cannot call StopRuntimeSession: runtime ARN unresolved (job %s)", job_id
        )
        return _cancel_failed(
            job_id, current_status, detail_prefix + "runtime ARN unresolved"
        )

    method = "stop_runtime_session"
    detail = detail_prefix.rstrip("; ") if detail_prefix else ""
    try:
        client = boto3.client("bedrock-agentcore", region_name=REGION)
        await asyncio.to_thread(
            client.stop_runtime_session,
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
        )
    except ClientError as err:
        err_info = err.response.get("Error", {})
        code = err_info.get("Code", "") or type(err).__name__
        message = err_info.get("Message", "") or str(err)
        if code == "ResourceNotFoundException":
            # The microVM is provably gone (expired or already stopped).
            method = "session_already_terminated"
            detail = (
                detail_prefix
                + f"runtime session {session_id} not found or already "
                f"terminated: {message}"
            )
        else:
            logger.warning(
                "StopRuntimeSession failed for job %s session %s: %s: %s",
                job_id, session_id, code, message,
            )
            return _cancel_failed(
                job_id, current_status,
                detail_prefix + f"StopRuntimeSession failed: {code}: {message}",
            )
    except Exception as exc:
        logger.warning(
            "StopRuntimeSession failed for job %s session %s: %s: %s",
            job_id, session_id, type(exc).__name__, exc,
        )
        return _cancel_failed(
            job_id, current_status,
            detail_prefix
            + f"StopRuntimeSession failed: {type(exc).__name__}: {exc}",
        )

    # ── 3. Record CANCELLED (conditional on the row still being RUNNING) ──
    conflict_status = await _record_cancelled(job_id, _user_id, record)
    if conflict_status == "CANCELLED":
        # The killed microVM's pipeline ran its CancelledError handler
        # during shutdown and won the race for the CANCELLED write. The
        # stop itself succeeded, so this is still a successful cancel.
        detail = f"{detail}; {_HANDLER_RECORDED_DETAIL}" if detail else _HANDLER_RECORDED_DETAIL
    elif conflict_status is not None:
        return _cancel_failed(
            job_id, conflict_status,
            "job reached a terminal state before the cancellation was recorded",
        )

    logger.info(
        "cancel_task job %s: method=%s runtime_session_id=%s",
        job_id, method, session_id,
    )
    result = {"job_id": job_id, "status": "CANCELLED", "method": method}
    if detail:
        result["detail"] = detail
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Fail fast if OPENCODE_BINARY is misconfigured, so a broken
    # container fails at startup instead of on the first coding tool
    # call. See container.tools.run_opencode_acp._validate_opencode_binary
    # for the contract.
    from container.tools.run_opencode_acp import (
        OPENCODE_BINARY,
        _validate_opencode_binary,
    )
    _validate_opencode_binary(OPENCODE_BINARY)

    logger.info("Starting FastMCP on port 8000")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
