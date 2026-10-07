<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Timeout Cascade

This document describes the timeout values at each layer of the OpenCode on AgentCore stack, the expected behavior when each layer times out, and the recommended timeout ordering to prevent orphaned background work.

## Timeout Layers

The request path flows through four layers, each with its own timeout:

```
Client --> Gateway --> Interceptor Lambda --> Runtime (container) --> Tool (OpenCode subprocess)
```

### 1. Gateway Idle Timeout

| Setting | Value |
|---------|-------|
| Discovery timeout (`tools/list`) | 20 seconds |
| Idle connection timeout | Managed by AgentCore Gateway service |

The Gateway is a managed AgentCore resource. Its idle timeout governs how long an open SSE or HTTP connection can remain idle before the Gateway closes it. The `tools/list` discovery timeout (20s) applies when the Gateway calls the Runtime during `CreateGatewayTarget` to enumerate available tools.

### 2. Runtime Session Timeout

| Setting | Value |
|---------|-------|
| Session lifetime | Managed by AgentCore Runtime service |
| Session storage | `/mnt/session` managed mount (`us-east-1` by default); work directories default to `/tmp/opencode-sessions` |

The Runtime is a managed AgentCore microVM. Session lifetime is controlled by the AgentCore service. The Runtime hosts a FastMCP Python server on port 8000 that processes MCP requests. No session lifecycle (idle timeout / maximum lifetime) is configured today, so the service defaults apply; adding `LifecycleConfiguration` to the Runtime ([`AWS::BedrockAgentCore::Runtime`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-runtime.html)) is a deferred follow-up.

### 3. Interceptor Lambda Timeout

| Setting | Value | Source |
|---------|-------|--------|
| Lambda timeout | **5 seconds** | `gateway_stack.py`: `timeout=cdk.Duration.seconds(5)` |
| Memory | 128 MB | `gateway_stack.py` |

The Interceptor is a REQUEST Lambda that extracts `user_id` from the JWT `sub` claim and injects it into tool call arguments. It runs on every inbound request before the request reaches the Runtime. The 5-second timeout is generous for this lightweight operation (base64 decode + JSON parse), which typically completes in under 100ms.

### 4. Tool-Level Timeout (`timeout_minutes`)

| Setting | Value | Source |
|---------|-------|--------|
| Default | **10 minutes** | `code_mcp_server.py`: `timeout_minutes: int = 10` (hardcoded default in both `code` and `run_coding_task` tool signatures) |
| Maximum | **30 minutes** | `code_mcp_server.py`: `timeout_minutes > 30` validation check |
| Allowed range | 1--30 minutes | `code_mcp_server.py`: validated on each call |

> **Note:** `cdk.json` contains `task_timeout_minutes_default` (10) and `task_timeout_minutes_max` (30) as reference values, but these are CDK context only -- they are not passed as environment variables to the container. The actual defaults and limits are hardcoded in the Python tool function signatures. If you change the values in `cdk.json`, you must also update the Python defaults in `code_mcp_server.py` to keep them in sync.

The tool-level timeout controls how long the OpenCode subprocess is allowed to run for a single coding task. When the timeout expires:

1. The container's Python code sends **SIGTERM** to OpenCode's whole process group (via `_terminate_process()` in `run_opencode_acp.py`; OpenCode is spawned with `start_new_session=True`, so it leads its own group and every process its bash tool started is in it)
2. A **5-second grace period** allows the process to clean up
3. If the leader has not exited, the container sends **SIGKILL** to the group
4. Once the leader is gone, the group gets one more **SIGKILL** so a background child does not outlive the run

Termination is signal-only: there is no exit confirmation for the rest of the group, and a descendant that started its own session with `setsid` is not covered. That is an accepted residual risk (see [THREAT-MODEL.md](THREAT-MODEL.md) RT-D): every process in the microVM already runs with the execution role, and the microVM is discarded at session end.

This timeout is set per-call via the `timeout_minutes` parameter on the `code` and `run_coding_task` tools. The timeout is enforced inside the container by `run_opencode_acp`, which calculates a deadline from `timeout_seconds` and raises `asyncio.TimeoutError` when the deadline is exceeded. The same `_terminate_process()` also runs in a `finally` block after a normal exit, so the group is killed before the pipeline restores `.git/config` and scans the tree.

#### Related Timeouts Inside the Container

| Setting | Value | Source |
|---------|-------|--------|
| Elicitation timeout | 300 seconds (5 min) | `code_mcp_server.py`: hardcoded default in `ELICITATION_TIMEOUT_S = int(os.environ.get("ELICITATION_TIMEOUT_S", "300"))`, overridable via env var (not set in CDK stacks) |
| SIGTERM to SIGKILL grace | 5 seconds | `run_opencode_acp.py`: `_terminate_process()` |

The elicitation timeout applies to the OAuth consent flow in both `connect_git_host` and the `code` tool's inline OAuth prompt. Both use the shared `_elicit_with_timeout` helper in `code_mcp_server.py`, which wraps `ctx.elicit()` with `asyncio.wait_for`.

## Expected Behavior When Each Layer Times Out

### Interceptor Times Out (5s)

- **What happens:** The Gateway receives no valid interceptor response.
- **Effect:** The Gateway rejects the request. The Runtime never sees it.
- **Risk:** None. The request fails cleanly at the edge. No background work is started.
- **Likely cause:** Lambda cold start issues or a bug in the interceptor code. Under normal operation this timeout is never hit.

### Tool Times Out (10--30 min)

- **What happens:** The container's Python code (`run_opencode_acp`) catches `asyncio.TimeoutError` and calls `_terminate_process()`, which sends SIGTERM to the process group, then SIGKILL after 5 s if the leader hasn't exited, then a final group SIGKILL.
- **Effect:** The coding task is marked as `FAILED` with a timeout error. The job record in DynamoDB is updated. Any partial work (uncommitted file changes) remains in the work directory but is not pushed.
- **Risk:** Low. The process is forcefully terminated. No orphaned compute.
- **Likely cause:** Complex coding tasks, large repositories, or model latency.

### Runtime Session Ends

- **What happens:** The AgentCore service stops the microVM.
- **Effect:** Any in-flight tool execution is terminated. The MCP connection drops. The client receives a connection error or timeout.
- **Risk:** Medium. If a tool was mid-execution, the job status may not be updated to `FAILED`. The DynamoDB record could remain in `RUNNING` state (stale).
- **Mitigation:** The table has no cross-user index, so stale `RUNNING` rows are found per user with `list_tasks` (status filter `RUNNING`) or via a table export; `cancel_task` on a stale job marks it `CANCELLED`.

### Gateway Idle Timeout

- **What happens:** The Gateway closes the idle HTTP/SSE connection.
- **Effect:** The client loses its connection. However, for async tasks (`run_coding_task`), the background pipeline continues running in the Runtime because it is decoupled from the request lifecycle.
- **Risk:** High for sync tasks (`code` tool) -- the client loses the response. Low for async tasks -- the background pipeline completes independently and updates DynamoDB.
- **Mitigation:** Use `run_coding_task` (async) for long-running operations. Use `get_task_status` to poll for results.

## Recommended Timeout Ordering

Timeouts should be ordered so that inner layers time out before outer layers:

```
tool timeout  <  Runtime session  <  Gateway idle timeout
(10-30 min)      (managed)           (managed)
```

**Why this ordering matters:**

1. **Tool < Runtime**: The tool should finish (or be killed) before the Runtime session ends. This ensures the job status is updated in DynamoDB and any cleanup (credential scanning, git push) can complete. If the Runtime dies first, the tool is killed without cleanup.

2. **Runtime < Gateway**: The Runtime should remain alive for the duration of the Gateway connection. If the Gateway times out first on a synchronous call, the Runtime may continue processing a request whose response can never be delivered. For async tasks this is less critical since results are stored in DynamoDB.

3. **Interceptor is independent**: The Interceptor timeout (5s) is a pre-processing step. It should always be much shorter than any other timeout since it only performs JWT extraction.

### Current Configuration Assessment

The current defaults follow the recommended ordering:

| Layer | Timeout | Order |
|-------|---------|-------|
| Interceptor Lambda | 5 seconds | Shortest (pre-processing) |
| Tool (`timeout_minutes`) | 10--30 minutes | Inner |
| Runtime session | Managed by service | Middle |
| Gateway idle | Managed by service | Outer |

The managed timeouts (Runtime session and Gateway idle) are controlled by the AgentCore service and are expected to exceed the tool-level timeout under normal operation. If you observe Gateway or Runtime timeouts before tool completion, check:

- Whether `task_timeout_minutes_max` (30 min) exceeds the Runtime session limit for your region
- Whether long-running SSE connections are being terminated by intermediate proxies or load balancers
