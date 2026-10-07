<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Tools

Reference for the six MCP tools the sample exposes through the AgentCore Gateway. All tools are routed to a single MCP Server target named `opencode`, so the effective Cedar action identifiers are `opencode___{tool}` with three underscores.

## Tool reference

| Tool | Mode | Description | Required parameters |
|------|------|-------------|---------------------|
| `code` | Sync | Execute coding task, relay progress notifications (1/5..5/5) to the client, return PR URL. Uses `ctx.elicit()` for OAuth consent if needed. | `task_description`, `repo_url`, `base_branch` |
| `run_coding_task` | Async | Submit task, get `job_id` immediately. Runs in background via AgentCore async tasks. No mid-task clarification. | `task_description`, `repo_url`, `base_branch` |
| `connect_git_host` | Sync | Connect a git host (GitHub) by completing OAuth via elicitation. Run before submitting coding tasks to a new host. | `git_host` |
| `get_task_status` | Sync | Return the job record for one `job_id` (see [Job record shape](#job-record-shape)). | `job_id` |
| `list_tasks` | Sync | List the caller's jobs as job records. Supports status filtering, capped at 100 results. Ordered by `job_id`, not by creation time. | - |
| `cancel_task` | Sync | Stop a running task via `StopRuntimeSession` (in-process when the job runs on the same microVM). Writes `CANCELLED` only when something was actually stopped; otherwise returns `cancel_failed`. | `job_id` |

For `code` and `run_coding_task`, `repo_url` must be an `https://` URL (SSH and `git@` URLs are rejected before any job record is written). `code` returns the rejection as its error; `run_coding_task` validates in the background after it has returned a `job_id`, so a rejected job never appears in `get_task_status`. `base_branch` must already exist on the remote; a missing branch fails the job with `base_branch '<name>' not found on remote <repo_url>`. The optional `target_branch` defaults to `opencode/{job_id}` and must differ from `base_branch`.

The pipeline always commits every change left in the working tree and opens a pull request against `base_branch`, regardless of task wording: asking the agent "do not commit" has no effect. The only case with no PR is when OpenCode leaves the tree unchanged (then `pr_url` is empty and nothing is pushed). `files_edited` lists paths relative to the repository root, and only files written by OpenCode's edit-kind tools; files the agent merely read are not included.

Cold start is roughly 1.2 s per microVM.

### Progress notifications (`code`)

The sync `code` tool reports progress at each pipeline phase: `1/5 Cloning repository...`, `2/5 Running OpenCode...`, `3/5 Scanning for credentials...`, `4/5 Pushing changes...`, `5/5 Done`. Two things must be true for those notifications to reach the client:

- the Gateway must have MCP response streaming enabled (`protocolConfiguration.mcp.streamingConfiguration.enableResponseStreaming`, set by `stacks/gateway_stack.py`); without it the Gateway returns a single `application/json` body with no notifications;
- the client must send `Accept: application/json, text/event-stream` and a `_meta.progressToken` on the `tools/call` request. FastMCP only emits `notifications/progress` when a progress token is present.

When both hold, the Gateway answers with `text/event-stream` carrying the five `notifications/progress` events followed by the result. Progress is also written to the Runtime log either way.

## Examples

### `code` - synchronous coding tool

```json
// Input
{
  "task_description": "Add dark mode toggle",
  "repo_url": "https://github.com/org/repo",
  "base_branch": "main"
}

// Output
{
  "status": "complete",
  "pr_url": "https://github.com/org/repo/pull/42",
  "stop_reason": "end_turn",
  "files_edited": ["src/components/DarkMode.tsx", "src/styles/theme.css"],
  "duration_seconds": 120
}
```

The PR contains one squashed commit with everything the agent changed, pushed to `target_branch` (default `opencode/<job_id>`) and opened against `base_branch`. `pr_url` is empty when the task produced no changes, since nothing is pushed, and when the branch was pushed but the PR could not be created. A failed run returns `{"status": "failed", "error": "...", "duration_seconds": ...}`, for example `base_branch 'no-such-branch' not found on remote https://github.com/org/repo` when the base branch does not exist. Sync jobs are recorded in DynamoDB like async ones (except when `repo_url` or a branch name is rejected before the record is written), so they appear in `list_tasks` too.

### `run_coding_task` - asynchronous coding tool

```json
// Input
{
  "task_description": "Migrate the payment module to the new v2 API",
  "repo_url": "https://github.com/org/repo",
  "base_branch": "main"
}

// Output (immediate)
{
  "job_id": "01HXYZ...",
  "status": "RUNNING"
}
```

Poll with `get_task_status` using the returned `job_id` to watch the job move through `RUNNING -> {COMPLETE | FAILED | CANCELLED}`. The same "always commit, always open a PR" behaviour as `code` applies.

### Job record shape

`get_task_status` and `list_tasks` return the same 13-field record (`JOB_PUBLIC_FIELDS` in [`container/lib/dynamodb_helpers.py`](../container/lib/dynamodb_helpers.py)), in this order. Missing string fields default to `""`, `files_edited` to `[]`, and `duration_seconds` to `0`. `duration_seconds` is always a JSON number. Internal attributes (`PK`, `SK`, `user_id`, `runtime_session_id`) are never returned.

```json
// get_task_status {"job_id": "5625d253-d2c4-403f-8bef-22cebe50f8bd"}
{
  "job_id": "5625d253-d2c4-403f-8bef-22cebe50f8bd",
  "status": "COMPLETE",
  "task_description": "Create a file TEST-5.md containing one line",
  "repo_url": "https://github.com/org/repo",
  "base_branch": "main",
  "target_branch": "opencode/5625d253-d2c4-403f-8bef-22cebe50f8bd",
  "pr_url": "https://github.com/org/repo/pull/26",
  "stop_reason": "end_turn",
  "files_edited": ["TEST-5.md"],
  "duration_seconds": 14.6,
  "error": "",
  "created_at": "2026-10-07T13:16:31.393509+00:00",
  "completed_at": "2026-10-07T13:16:45.912661+00:00"
}
```

`list_tasks` wraps the records as `{"jobs": [...], "count": N}`. Its order follows the DynamoDB sort key `job#{job_id}#{created_at}` descending, which sorts by `job_id` (a UUID) rather than by creation time; sort client-side on `created_at` if you need chronological order. Unknown `job_id` values return `{"error": "Job not found"}`.

### `cancel_task` - stop a running job

```json
// Input
{ "job_id": "505b83e7-1235-425c-9472-b92f8a87c6f4" }

// Output - the microVM running the job was stopped
{ "job_id": "505b83e7-1235-425c-9472-b92f8a87c6f4", "status": "CANCELLED", "method": "stop_runtime_session" }
```

Only the job's owner can cancel it. The response says what actually happened:

| Outcome | Response | Job record |
|---------|----------|------------|
| Stopped, or provably no longer running | `{"job_id", "status": "CANCELLED", "method": "in_process" \| "stop_runtime_session" \| "session_already_terminated"}` plus an optional `detail` string | updated to `CANCELLED`. For `stop_runtime_session` / `session_already_terminated` the tool writes `error` = `Task cancelled by user` and `duration_seconds` = seconds since `created_at`; for `in_process` the pipeline's own cancellation handler writes `error` = `Task cancelled`. |
| Nothing could be stopped | `{"job_id", "status": <current status, usually "RUNNING">, "error": "cancel_failed", "detail": <reason>}` | not modified; the task may still be running |
| Unknown job | `{"error": "Job not found"}` | - |
| Already finished | `{"error": "Job is already in terminal state: <STATUS>"}` | - |

`method` values: `in_process` when the job was running on the same microVM as the `cancel_task` call and its asyncio task was cancelled directly; `stop_runtime_session` when the Runtime control-plane `StopRuntimeSession` API terminated the microVM recorded on the job; `session_already_terminated` when `StopRuntimeSession` reported the session as not found (the job cannot be running any more, so the record is still marked `CANCELLED`).

`cancel_failed` details you may see: `job record has no runtime_session_id; cannot locate the microVM running it` (record written before the session id was captured), `runtime ARN unresolved`, `StopRuntimeSession failed: <code>: <message>`, and `job reached a terminal state before the cancellation was recorded` (the job ended `COMPLETE` or `FAILED` before the stop took effect). If instead the stopped microVM's pipeline recorded `CANCELLED` itself before `cancel_task` could, the response is still the success shape with `detail` = `CANCELLED was recorded by the job's own cancellation handler` (the record's `error` is then `Task cancelled`). See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#cancel_task-returns-cancel_failed).

Through the Gateway every `tools/call` lands on a new Runtime session and therefore a new microVM, so in practice the in-process path is never taken and `stop_runtime_session` is the normal result. The `CANCELLED` write is conditional on the record still being `RUNNING`, and the pipeline re-reads the record before its push step, so a late `COMPLETE` cannot overwrite a cancellation. A push that was already in flight in the last seconds of a job may still open a PR; the record stays `CANCELLED` in that case.

### `connect_git_host` - interactive OAuth consent

```json
// Input
{ "git_host": "github.com" }

// Output
{
  "status": "connected",
  "git_host": "github.com",
  "message": "Successfully connected to github.com."
}
```

Run this once per git host before submitting coding tasks. Other `status` values: `already_connected` when a token is already vaulted; `action_required` (with `authorization_url`) when the client does not support elicitation, the prompt times out after 300 s, or the user cancels, in which case open the URL and call the tool again; `failed` with a `message` when no credential provider is registered for the host (run `scripts/setup-oauth-app.sh`) or the authorization was not detected. The async pipeline cannot pause for OAuth mid-job, so it fails fast with `git_host_not_connected` if credentials are missing.

## Cedar policy action names

Because the Gateway registers a single MCP Server target named `opencode`, Cedar policies reference these action identifiers (three underscores between target name and tool name):

- `opencode___code`
- `opencode___run_coding_task`
- `opencode___connect_git_host`
- `opencode___get_task_status`
- `opencode___list_tasks`
- `opencode___cancel_task`

`scripts/create-policies.py` creates six policies on these actions:

- `opencode_readonly_deny_coding` - forbid `run_coding_task` for the `readonly` role
- `opencode_readonly_deny_cancel` - forbid `cancel_task` for the `readonly` role
- `opencode_readonly_deny_code` - forbid `code` for the `readonly` role
- `opencode_deny_production_repos` - forbid `code` and `run_coding_task` for `repo_url` values ending in `-production`, `-production.git`, or `-production/`
- `opencode_permit_full_access` - permit the `admin` and `developer` roles on all six actions
- `opencode_permit_readonly_status` - permit the `readonly` role on `get_task_status` and `list_tasks`

The role is read from the principal tag `custom:role`, populated from the Cognito ID-token claim of the same name. Cedar is default-deny, so under ENFORCE a caller with no recognised role is denied every action.

The script takes a single `--region` argument and reads `PolicyEngineId` and `GatewayArn` from the `OpenCodeGateway` stack outputs. The Gateway evaluates the policies in LOG_ONLY mode by default. See [HARDENING.md](HARDENING.md#cedar-policy-engine) for how to verify decisions and switch Cedar from LOG_ONLY to ENFORCE.
