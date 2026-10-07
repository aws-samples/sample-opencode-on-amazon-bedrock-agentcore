<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Threat Model

This document is the security analysis for the OpenCode on Amazon Bedrock AgentCore sample. It enumerates trust boundaries, data flows, STRIDE threats per component, GenAI-specific threats, and the residual risks the sample accepts by design. Pair it with [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the component walkthrough and [docs/HARDENING.md](HARDENING.md) for the concrete controls a production adopter is expected to add.

---

## Purpose and scope

This threat model exists to:

- Make the security posture of the sample reviewable in one document.
- Map every credible threat to a concrete control, a residual-risk acknowledgement, or a customer responsibility.
- Give production adopters a starting checklist rather than a hand-wave.

In scope: everything synthesized by the six CDK stacks plus the container image built from [`container/`](../container/) and the two post-deploy scripts that configure AgentCore resources ([`scripts/create-policies.py`](../scripts/create-policies.py), [`scripts/setup-oauth-app.sh`](../scripts/setup-oauth-app.sh)). Out of scope: the AWS services that the sample integrates with (Amazon Bedrock AgentCore, Amazon Bedrock, AWS KMS, Amazon Cognito, AWS Secrets Manager, Amazon DynamoDB, Amazon CloudWatch, Amazon S3, Amazon ECR, AWS Lambda, Amazon API Gateway, Amazon VPC) are assumed to operate as documented. GitHub is a third-party dependency.

## Assumptions

The threat model is only as good as the assumptions under it. These are the assumptions we rely on; each one is an explicit invitation for reviewers to push back.

1. **AWS infrastructure is trustworthy.** AWS services enforce the controls AWS documents. KMS encrypts what we tell it to encrypt, CloudTrail logs what we tell it to log, and so on.
2. **The customer's AWS account is not compromised.** The root of trust is the account boundary. A compromised account operator can bypass every control the sample adds.
3. **The deployer reviews and approves the template before `cdk deploy`.** This is a sample repository, not a managed service. Customers read the code.
4. **Upstream package and binary integrity is out of scope.** Python packages from PyPI via `container/requirements.txt` and base container images from the public Docker registry are trusted to be what they claim. OpenCode is a pinned release (1.18.34) whose `linux-arm64` tarball is downloaded from the `anomalyco/opencode` GitHub release and checked with `sha256sum -c` against a pinned checksum at build time (no `curl | bash`); the release itself is trusted. Production adopters are expected to layer on whatever supply-chain controls they need.
5. **Cognito users are provisioned by a trusted operator.** `self_sign_up_enabled=False` and the user pool is operator-managed. We do not model the case where a malicious user is admitted.
6. **The MCP client is trusted.** If the client is compromised, nothing about this sample's defences protects the user. Clients are documented with config guidance in [docs/MCP-CLIENTS.md](MCP-CLIENTS.md).
7. **GitHub enforces its own access controls.** Repo-level access is enforced by the git provider via the user's OAuth token, not by this sample.
8. **OpenCode runs with the Runtime execution role.** The agent process inherits the same AWS credentials as the FastMCP server (see OC-E). The git-hardening controls in the pipeline (PL-T3) protect the integrity of the push and PR; they are not, and do not claim to be, a credential boundary between the agent and the execution role.

## System overview

See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the full component walkthrough and sequence diagrams. For the threat model, the relevant top-level flow is:

```
MCP Client
   │   (Cognito JWT, Authorization header)
   ▼
Amazon Bedrock AgentCore Gateway
   │   (JWT validated by Gateway; Cedar policy evaluated)
   │   (REQUEST interceptor extracts user_id; strips inbound Authorization header)
   │   (SigV4 signed with GATEWAY_IAM_ROLE)
   ▼
Amazon Bedrock AgentCore Runtime (per-session Firecracker microVM)
   │   FastMCP server :8000
   │   5-step pipeline: credential resolve → clone → OpenCode → scan → push
   │
   ├──► Amazon Bedrock (LLM inference)
   ├──► GitHub (clone, push, create PR; over NAT Gateway)
   ├──► Amazon DynamoDB (audit records; KMS-encrypted)
   ├──► AWS Secrets Manager (AgentCore Identity token vault; KMS-encrypted)
   └──► Amazon CloudWatch Logs (KMS-encrypted)

OAuth 3LO flow (out-of-band):
User's browser ─► GitHub ─► API Gateway HTTP API ─► Callback Lambda ─► AgentCore Identity
                                   │
                                   └─ HttpLambdaAuthorizer validates query-string shape
```

## Data inventory and sensitivity

| Data | Where it lives | Sensitivity | Encrypted at rest | Encrypted in transit |
|------|----------------|-------------|-------------------|----------------------|
| Cognito ID tokens (JWTs) | MCP client config, HTTP headers | Medium (24 h TTL) | Client's responsibility | TLS (client → Gateway) |
| OAuth app credentials (GitHub client secret) | AWS Secrets Manager (`opencode/github-oauth-app`, written by `scripts/setup-oauth-app.sh`) and the AgentCore Identity credential provider | High | AWS-managed Secrets Manager key (script does not pass a CMK) | TLS (AWS CLI) |
| User OAuth refresh tokens | AgentCore Identity Vault (`bedrock-agentcore-identity*` secrets) | High | AWS-owned key by default; CMK configurable | TLS (AgentCore Identity SDK) |
| User OAuth access tokens (in-flight) | Runtime microVM memory, `GIT_ASKPASS` sidecar file (mode `0o400`) | High | In-memory only; sidecar removed in `finally` block | N/A (local) |
| Coding task description | HTTP request, Runtime memory, Bedrock prompts, `opencode-jobs` DynamoDB table (`task_description` field) | Medium (may contain user PII or repo info) | AgentCore session encryption (Bedrock); customer-managed CMK (DynamoDB) | TLS |
| Cloned repository contents | Runtime microVM ephemeral filesystem (work directories under `SESSION_STORAGE_PATH`, default `/tmp/opencode-sessions`; not on the managed session storage mount today) | High (customer code) | Ephemeral microVM filesystem (no sample-configured encryption); session storage encryption would apply only if work directories move to the mount | TLS (git over HTTPS) |
| LLM output (generated code + commentary) | Runtime microVM memory; pushed to GitHub after credential scan | Medium | N/A (transient) | TLS (git push) |
| DynamoDB audit records | `opencode-jobs` table | Medium (user_id, job_id, status, task_description, repo_url, base/target branch, runtime_session_id, timestamps; on completion pr_url, error, stop_reason, files_edited, duration; no repo contents) | Customer-managed CMK | TLS |
| CloudWatch Logs (Runtime, Gateway interceptor, Lambdas) | AgentCore-managed Runtime log groups (`/aws/bedrock-agentcore/runtimes/*`); stack-created log groups for the interceptor, OAuth callback, authorizer, HTTP API access log, and VPC flow logs | Medium (may contain user_id, job_id, repo URL, branch names, error traces, streamed OpenCode stderr at INFO, and a stderr tail on failure; the OAuth callback logs presence flags, the target URL, and HTTP status; on an upstream HTTP error it logs the status code and request ID, and on other errors the exception text (see CB-I)) | Customer-managed CMK for stack-created log groups; AWS-managed encryption for the Runtime log groups | TLS |
| CloudTrail events (optional) | Customer-managed S3 bucket | High (audit log) | Customer-managed CMK | TLS |

## Trust boundaries

1. **Account boundary** - everything inside the customer's AWS account. Actor: customer operator. Boundary controls: AWS account authentication, IAM.
2. **Inbound MCP boundary** - between the untrusted public internet and the Gateway. Boundary controls: Amazon Bedrock AgentCore Gateway's JWT authorizer (`CustomJwtAuthorizer`), Cedar Policy Engine (LOG_ONLY by default, switchable to ENFORCE), TLS.
3. **Gateway → Runtime boundary** - between the Gateway and the Runtime microVM. Boundary controls: SigV4 with `GATEWAY_IAM_ROLE`, REQUEST interceptor Lambda ([`lambda/interceptor/index.py`](../lambda/interceptor/index.py)) strips inbound `Authorization` header, always discards any client-supplied `_user_id`, injects `_user_id` from the validated JWT `sub` claim, and rejects a `tools/call` with no derivable identity.
4. **Per-session microVM boundary** - each Runtime invocation runs in its own Firecracker microVM with an ephemeral filesystem. Boundary controls: AgentCore Runtime session isolation.
5. **OpenCode subprocess boundary** - the OpenCode binary runs as a child process of the FastMCP server inside the microVM. Boundary controls: process isolation, validated absolute path to the binary ([`container/tools/run_opencode_acp.py`](../container/tools/run_opencode_acp.py) `_validate_opencode_binary`), startup-time fail-fast. This is not a credential boundary: OpenCode inherits the server's environment plus the execution role's AWS credentials (see OC-E). Config is passed inline via `OPENCODE_CONFIG_CONTENT`, and any inherited `OPENCODE_CONFIG` path is removed from the child environment.
6. **OAuth callback boundary** - between the user's browser (coming back from GitHub) and the callback Lambda. Boundary controls: HTTP API Gateway with an `HttpLambdaAuthorizer` ([`stacks/callback_api_stack.py`](../stacks/callback_api_stack.py)) that validates `session_id` shape and `state`-JSON structure; TLS. The authorizer is a structural check only; the callback is not bound to an authenticated browser session (see CB-S).
7. **VPC egress boundary** - Runtime outbound traffic leaves the VPC through the NAT Gateway (or through VPC endpoints for AWS services). Boundary controls: the Runtime security group has no allow-all rule and exactly one egress rule, TCP 443 to `0.0.0.0/0` (IPv4); VPC endpoints for AWS services. Port 443 alone does not restrict destinations, and DNS queries to the Route 53 Resolver cannot be filtered by security groups. **FQDN-level and DNS egress filtering are documented residual risks** (see [docs/HARDENING.md#known-limitations](HARDENING.md#known-limitations)).
8. **Bedrock inference boundary** - LLM prompts and responses cross into the Bedrock service plane. Boundary controls: IAM scoped to specific model ARNs in [`stacks/agentcore_stack.py`](../stacks/agentcore_stack.py); Bedrock's upstream content filters and safety controls.

## Actors and assets

| Actor | Trust | Primary assets they touch |
|-------|-------|----------------------------|
| End user (via MCP client) | Semi-trusted (authenticates via Cognito, scoped by Cedar) | Coding task description, OAuth consent, git repo they own |
| Customer operator | Trusted (root in the account) | All AWS resources, CMK, Cedar policies, Cognito users |
| MCP client (Kiro, Claude Desktop, Cursor) | As trusted as the user running it | JWT, MCP traffic |
| GitHub (third party) | External (assumed to enforce its own access controls) | Clone payloads, push targets, OAuth tokens |
| Amazon Bedrock model (LLM) | Semi-trusted (pre-approved model, but output must be treated as untrusted) | Task descriptions (prompts), generated code (output) |
| OpenCode binary | Semi-trusted (pinned, SHA-256-verified upstream release installed at build time; executes LLM output in a microVM) | File system in `work_dir`, LLM-generated edit instructions |
| Attacker on the public internet | Hostile | May attempt: Gateway endpoint enumeration, OAuth callback replay, token theft via phishing |
| Attacker in a compromised MCP client | Hostile | Has the user's JWT; model this as the user |

## STRIDE analysis

Per-component threat → control mapping. The control either (a) mitigates the threat, (b) is a residual risk with an explicit acknowledgement, or (c) is a customer responsibility called out here and in HARDENING.md.

### 1. MCP Client → Gateway

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| MC-S | Spoofing | Attacker presents a forged JWT | Gateway validates JWT signature/issuer/audience via `CustomJwtAuthorizer` bound to the Cognito user pool. Cognito uses RS256; forgery requires the private key held by AWS. |
| MC-T | Tampering | Attacker modifies MCP request in transit | TLS between client and Gateway. Gateway validates the full request before forwarding. |
| MC-R | Repudiation | User denies submitting a task | Every tool call is attributed to the JWT `sub` claim (`_user_id` injected by the interceptor) and recorded in DynamoDB with timestamps. Optional CloudTrail captures the API-level event. |
| MC-I | Information disclosure | JWT is exfiltrated from the client | JWT TTL is 24 h. Client-side storage is documented in MCP-CLIENTS.md; "Option A" (auto-refresh wrapper) avoids on-disk storage. **Residual risk**: if the client is compromised, the attacker can act as the user for 24 h. Mitigated operationally by rotating Cognito user credentials. |
| MC-D | Denial of service | Attacker floods the Gateway | AgentCore Gateway handles service-level rate limiting. **Customer responsibility**: add WAF rules if the Gateway is exposed to the public internet. |
| MC-E | Elevation of privilege | Low-privilege role invokes a high-privilege tool | Cedar policies bound to `opencode___{tool}` action ARNs: permits are role-gated (`admin` / `developer` on the six tools, `readonly` on `get_task_status` and `list_tasks` only), and the `readonly` role is also forbidden from `code`, `run_coding_task`, and `cancel_task`. A caller with no recognised role matches no permit and is denied under `ENFORCE`. The role comes from the `custom:role` ID-token claim, which the app client can read but not write (roles are set only through Cognito admin APIs); there are no Cognito groups. The policies read the role from the principal tag `custom:role` only, guarded with `hasTag`, so a missing tag evaluates to no match rather than an error. **Residual risk**: Cedar engine runs in `LOG_ONLY` mode by default. If the role tag does not reach the policies (wrong claim, stale token, user without a role), no permit matches and `ENFORCE` denies every tool to every user (fails closed). **Customer responsibility**: confirm the role-gated ALLOW decisions in `LOG_ONLY` before switching to `ENFORCE` (a hard gate), per HARDENING.md. |

### 2. Gateway REQUEST interceptor ([`lambda/interceptor/index.py`](../lambda/interceptor/index.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| GI-S | Spoofing | Interceptor forwards a request with a fake `_user_id` | The interceptor only injects `_user_id` from the `sub` claim of a JWT the Gateway has already validated (no other claim is used as a fallback). A `tools/call` with no derivable identity (missing or unparseable token, no `sub`) is rejected with a 401-style interceptor error. |
| GI-T | Tampering | Client smuggles a pre-set `_user_id` in tool arguments | The interceptor always removes any client-supplied `_user_id` from tool arguments before injecting the JWT-derived value, including when the call is then rejected. |
| GI-I | Information disclosure | JWT is logged to CloudWatch | The interceptor reads the JWT claims but does not log the raw token. Forwarded headers exclude `Authorization` (required for correctness anyway - see MC-S below). |
| GI-E | Elevation of privilege | Inbound JWT overrides outbound SigV4 signature | The interceptor strips the inbound `Authorization` header before returning `transformedGatewayRequest.headers`, so the Gateway's SigV4 signature reaches the Runtime unchallenged. This is critical for `GATEWAY_IAM_ROLE` correctness; see [docs/ARCHITECTURE.md#architectural-decisions](ARCHITECTURE.md#architectural-decisions). |

### 3. Cedar Policy Engine ([`stacks/gateway_stack.py`](../stacks/gateway_stack.py), [`scripts/create-policies.py`](../scripts/create-policies.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| CP-T | Tampering | Attacker edits Cedar policies | Policies are created post-deploy via [`scripts/create-policies.py`](../scripts/create-policies.py) using IAM-authenticated API calls. Only principals with `bedrock-agentcore:CreatePolicy/UpdatePolicy` can modify them. |
| CP-R | Repudiation | A denied call is not recorded | `LOG_ONLY` mode writes evaluation records to CloudWatch. `ENFORCE` mode adds a hard deny plus the same log entry. |
| CP-E | Elevation of privilege | A missing policy allows an unintended action | Six bundled policies: three `readonly` forbids (`code`, `run_coding_task`, `cancel_task`), a forbid on `code` / `run_coding_task` when `repo_url` matches `*-production`, `*-production.git`, or `*-production/`, and two role-gated permits for `AgentCore::OAuthUser` principals (`opencode_permit_full_access`: `admin` / `developer` on the six tools; `opencode_permit_readonly_status`: `readonly` on `get_task_status` and `list_tasks`). Cedar is default-deny and forbid wins, so under `ENFORCE` any role/tool pair not covered by a permit is denied. The forbids are applied before the permits so a re-run against an `ENFORCE` gateway never has a permit active without its forbids; the script only ever deletes policies in a `FAILED` state. Roles are assigned only via Cognito admin APIs because the app client's `WriteAttributes` exclude `custom:role`. **Residual risk**: the production forbid is a case-sensitive suffix match on the raw submitted `repo_url` string, a guardrail rather than an access boundary; the git provider's OAuth token remains the access boundary. **Customer responsibility**: add organization-specific permits/forbids; verify coverage in `LOG_ONLY` before switching to `ENFORCE`. |

### 4. Gateway → Runtime (SigV4)

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| GR-S | Spoofing | Someone other than the Gateway signs a request to the Runtime | Runtime validates SigV4 against `GATEWAY_IAM_ROLE`. Forging requires the Gateway's role credentials. |
| GR-T | Tampering | Request body is modified in flight | SigV4 covers method, URL, headers, and body hash. Any tampering breaks the signature. |
| GR-I | Information disclosure | Runtime responses leak to a third party | Runtime → Gateway traffic is over TLS inside the AWS network. |

### 5. Runtime microVM (FastMCP server, [`container/code_mcp_server.py`](../container/code_mcp_server.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| RT-S | Spoofing | One session acts as another | Each session runs in its own Firecracker microVM with its own `session_id`. Tool calls carry `_user_id` from the interceptor. DynamoDB records are partitioned by `user#{user_id}`. |
| RT-T | Tampering | An attacker with in-process access modifies `_running_tasks` or `_cancel_flags` | In-process attack requires prior code execution inside the microVM - covered by OC-* and PL-* below. |
| RT-R | Repudiation | Runtime denies a job ever ran | DynamoDB RUNNING → terminal state transitions are idempotent and timestamped. |
| RT-I | Information disclosure | Logs or metrics leak sensitive data | CloudWatch log groups and the DynamoDB table are encrypted with the customer-managed CMK. OTEL metrics do not include request bodies. The prompt is logged only as its length. **Residual risk**: task descriptions are stored in the DynamoDB job record, and repo URLs, branch names, and git/OpenCode error text appear in logs (OpenCode stderr is streamed to the Runtime log at INFO; on a non-zero exit a stderr tail is logged at ERROR and stored in the job record's `error` field); document as "medium sensitivity". |
| RT-D | Denial of service | Async task never terminates | Each async task has a configurable per-call timeout (`timeout_minutes` default 10, maximum 30). OpenCode is spawned in its own session and process group; on timeout the group gets SIGTERM, then SIGKILL after a 5-second grace period, then one more group SIGKILL after the leader exits. **Residual risk**: a descendant that calls `setsid` leaves the group and is not signalled; it dies with the microVM at session end and holds no credentials beyond the execution role every process in the microVM already has (OC-E). |
| RT-E | Elevation of privilege | Tool call elevates beyond its declared action | Every tool signature validates inputs (`_validate_repo_url`, `_validate_git_ref` in [`container/pipeline.py`](../container/pipeline.py)). The execution role uses SigV4 scoped actions; see IR-* below. |

### 6. OpenCode subprocess ([`container/tools/run_opencode_acp.py`](../container/tools/run_opencode_acp.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| OC-T | Tampering | `OPENCODE_BINARY` env var points at an attacker binary | `_validate_opencode_binary` (called at FastMCP startup) requires an absolute path, a regular file, and executable bit. If the file is swapped between startup and first invocation, the microVM's root filesystem ACLs apply. |
| OC-T2 | Tampering | LLM output modifies files outside `work_dir` | OpenCode operates inside a per-job `work_dir` in the session's microVM. **Residual risk**: the microVM does not enforce a chroot on OpenCode; destructive filesystem commands would affect only that session's microVM filesystem, which is discarded at session end. Credentials are a separate concern (see OC-E). |
| OC-I | Information disclosure | LLM output leaks credentials into PRs | `scan_and_strip_credentials.py` runs after OpenCode and before `git push`. It scans files changed in agent self-commits (`base_sha..HEAD`), staged and unstaged edits, and untracked files; a file-discovery failure fails the job. Patterns covered today: AWS access keys (`AKIA`, `ASIA`), `sk-` API keys, GitHub tokens (`gh[pousr]_`, `github_pat_`), GitLab PATs (`glpat-`), PEM private keys, and high-entropy `secret=` / `password=` / `token=` / `key=` assignments. **Residual risk**: the scanner is regex-based. Credentials in formats it does not recognize pass through. Extending the regex set is called out in HARDENING.md. |
| OC-E | Elevation of privilege | OpenCode holds broader credentials than one task needs | `_resolve_aws_credentials_into_env` in [`container/tools/run_opencode_acp.py`](../container/tools/run_opencode_acp.py) resolves the Runtime execution role's credentials and passes them to the OpenCode child as environment variables; the child otherwise inherits the server's environment. The same role credentials are also reachable inside the microVM through the metadata service. **Residual risk**: the credentials are not scoped to one session's work directory or one task. The agent process can call any API the execution role allows, including the AgentCore Identity token APIs granted to the role (`GetWorkloadAccessTokenForUserId`, `GetResourceOauth2Token`). AWS recommends denying `GetWorkloadAccessTokenForUserId` where JWTs are available ([runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)). Root fix (user-bound workload tokens instead of `ForUserId`, a narrower role for the agent process) is deferred to a design spike. |
| OC-E2 | Elevation of privilege | Agent asks for permissions the headless pipeline cannot review | ACP `session/request_permission` requests are rejected (`reject_once`, else `reject_always`, else `cancelled`), and any other agent-to-client request gets JSON-RPC "method not found". The inline config already allows `edit` and `bash`, so this is a fallback. |

### 7. Pipeline ([`container/pipeline.py`](../container/pipeline.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| PL-T | Tampering | `repo_url` or branch name carries argv-flag smuggling (e.g. `--upload-pack=...`) | `_validate_repo_url` accepts only `https://` URLs and rejects NULs, whitespace, and oversize values. `_validate_git_ref` rejects leading `-` (argv-flag confusion), embedded whitespace, and oversize values. Subprocess invocation uses list-form argv throughout, so there is no shell-injection vector regardless of input. |
| PL-T2 | Tampering | Task description carries prompt-injection payload | **Residual risk**: task descriptions are forwarded to Bedrock verbatim. The system relies on the upstream model's safety training plus Cedar policies plus the credential scanner on output. See "GenAI-specific threats" below. |
| PL-T3 | Tampering | Agent-modified `.git` state (hooks, fsmonitor, credential helper, `url.*` rewrites, rewritten `origin`, background processes left running) influences what is pushed or where | **Goal: push integrity.** The branch that lands on the validated `repo_url` is exactly the tree the scanner inspected, and no repository hook or credential helper runs during the push. Controls ([`container/lib/git_safety.py`](../container/lib/git_safety.py)): (1) the pipeline snapshots `.git/config` after clone and branch setup and writes it back after OpenCode's process group has been killed, discarding every repository-local config change (remotes, `url.*.insteadOf`, `include.path`); (2) every git call in the scan and push steps runs with `core.hooksPath=/dev/null`, an empty `core.fsmonitor` and `credential.helper`, inherited `GIT_CONFIG_*` dropped, system and global config pointed at `/dev/null`, `GIT_TERMINAL_PROMPT=0`, and `GIT_ALLOW_PROTOCOL=https`; (3) the push URL is derived from the validated `repo_url`, not from `.git/config`, and the token is supplied only through a short-lived `GIT_ASKPASS` script; (4) OpenCode's whole process group is SIGTERM/SIGKILLed before the restore so no agent-started process edits the tree during the scan and push. **Trust assumption / residual risk**: OpenCode runs with the Runtime execution role (OC-E), so these controls are not a credential boundary and do not prevent the agent from using that role while it runs. A descendant that detaches with `setsid` escapes the process-group kill (RT-D); the `.git/config` restore happens before the scan either way. |
| PL-I | Information disclosure | OAuth token written to a tempfile readable by other processes | `container/lib/git_askpass.py` uses `os.open(..., mode=0o400)` on the sidecar token file and `os.chmod(..., 0o500)` on the askpass script itself. Both are removed in `finally` blocks. Tests lock this invariant ([`tests/unit/test_git_askpass_permissions.py`](../tests/unit/test_git_askpass_permissions.py)). |
| PL-I2 | Information disclosure | Intermediate agent commits carry content the scanner redacted | Agent self-commits are included in the scan (`base_sha..HEAD`), and the push step squashes everything since `base_sha` into one commit built from the scanned tree, so intermediate commits are not pushed. |
| PL-R | Repudiation | A job's terminal state is not attributable | Terminal-state writes to DynamoDB are guarded by the `user_id` from the JWT-derived `_user_id`, not from the request body. Idempotent within a job. |

### 8. Runtime execution role ([`stacks/agentcore_stack.py`](../stacks/agentcore_stack.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| IR-E | Elevation of privilege | Overly broad role lets a compromised container do more than intended | Wildcards on the role, as listed in the cdk-nag `IAM5` suppression: CloudWatch Logs/Metrics `*` (service-required for `PutMetricData` and `DescribeLogGroups`, convenience for the log-group/stream actions); X-Ray `*` (service-required); ECR `GetAuthorizationToken` `*` (service-required); ECR `repository/*` (convenience, CDK bootstrap asset repository); Bedrock `foundation-model` Region wildcard on the pinned model ID (required for a geo/global inference profile, convenience otherwise; DynamoDB is pinned to `table/opencode-jobs` with no wildcard); AgentCore `arn:aws:bedrock-agentcore:<region>:<account>:*` (convenience); Secrets Manager `bedrock-agentcore-identity*` (prefix-scoped); KMS `GenerateDataKey*` / `ReEncrypt*` action wildcards on the one CMK. There is no `sts:AssumeRole` statement and no per-task scoped credential. **Customer responsibility**: narrow the convenience wildcards for production; see [HARDENING.md#execution-role-wildcards](HARDENING.md#execution-role-wildcards). |
| IR-I | Information disclosure | Role reads secrets beyond its scope | Secrets Manager access is restricted to `bedrock-agentcore-identity*`. The sample's own secrets (`opencode/webhook-signing-secret`, `opencode/github-oauth-app`) live under the `opencode/*` prefix; neither the Runtime role nor the callback Lambda can read them (the Lambda is also limited to `bedrock-agentcore-identity*`). |

### 9. OAuth 3LO callback ([`stacks/callback_api_stack.py`](../stacks/callback_api_stack.py), [`lambda/oauth_callback/index.py`](../lambda/oauth_callback/index.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| CB-S | Spoofing | Attacker replays an old callback URL | The `HttpLambdaAuthorizer` ([`stacks/callback_api_stack.py`](../stacks/callback_api_stack.py)) validates `session_id` shape (regex) and requires `state` to be JSON with a `user_id` key. AgentCore Identity validates `session_id` is one it issued; the `CompleteResourceTokenAuth` call fails for unknown sessions. **Residual risk**: the callback is not bound to an authenticated browser session, and the `state` value is validated for shape only. Binding is deferred pending a consent-portal design spike. |
| CB-T | Tampering | Attacker modifies query-string params in flight | The callback URL is served over TLS by API Gateway. |
| CB-R | Repudiation | No audit trail of OAuth consents | API Gateway access logs are written to a KMS-encrypted CloudWatch log group with request-id, source IP, and timestamp. |
| CB-I | Information disclosure | Authorization code is leaked | The `HttpLambdaAuthorizer` runs synchronously before the callback Lambda; requests that fail its structural check never reach the Lambda. The callback Lambda and authorizer log presence flags for `session_id` / `state`, the target URL, and HTTP status, not query values; on an upstream HTTP error the Lambda logs only the status code and request ID. The API Gateway access log format uses [`$context.path`](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-logging-variables.html) and includes no query-string variable. Responses are HTML-escaped and carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. **Residual risk**: on an upstream HTTP error the escaped upstream error body is shown in the browser response, and on other errors the Lambda logs and returns the exception text. |
| CB-E | Elevation of privilege | Callback registers a token for a different user | `state` carries the originating `user_id`; AgentCore Identity associates the resulting token with that user. **Residual risk**: as in CB-S, the callback is not bound to the browser session that started the flow; binding is deferred. |

### 10. Amazon Cognito user pool ([`stacks/security_stack.py`](../stacks/security_stack.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| CG-S | Spoofing | Attacker registers a rogue user | `self_sign_up_enabled=False`. Users are admin-provisioned. |
| CG-I | Information disclosure | Weak password allows guessing | Password policy requires min length 12, lower + upper + digit + symbol. Standard threat protection is enabled (`StandardThreatProtectionMode.FULL_FUNCTION`). |
| CG-E | Elevation of privilege | Credential-stuffing attack succeeds | **Residual risk**: MFA is not enforced on the sample pool. **Customer responsibility**: enable Cognito MFA before routing real users through this pool; documented in HARDENING.md. |

### 11. DynamoDB audit records ([`stacks/job_store_stack.py`](../stacks/job_store_stack.py), [`container/lib/dynamodb_helpers.py`](../container/lib/dynamodb_helpers.py))

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| DB-T | Tampering | Attacker rewrites an existing record | Table uses the customer-managed CMK. IAM restricts writes to the Runtime execution role. Records are partitioned by `user#{user_id}`. |
| DB-I | Information disclosure | Cross-user record read | Queries use `PK = user#{user_id}` sourced from the JWT-derived `_user_id`, not from request body fields. |
| DB-D | Denial of service | A single user floods the table with job records | The table is on-demand (`PAY_PER_REQUEST`) with no secondary index, partitioned by `user#{user_id}`, so one user's volume stays in that user's partition. `list_tasks` caps results at 100. **Customer responsibility**: rate-limit submissions at the Gateway or via Cedar if abuse is a concern. |

### 12. VPC egress

| ID | STRIDE | Threat | Control |
|----|--------|--------|---------|
| EG-I | Information disclosure | Runtime exfiltrates data to an attacker-controlled host | The Runtime security group has exactly one egress rule, TCP 443 to `0.0.0.0/0`, and no allow-all rule. AWS service traffic uses VPC endpoints. **Residual risk**: 443-only egress does not prevent exfiltration to arbitrary HTTPS hosts; non-AWS traffic (git hosts, but also any public HTTPS endpoint the OpenCode binary or a compromised model prompt chooses) is unfiltered through the NAT Gateway, and DNS queries to the Route 53 Resolver cannot be filtered by security groups ([VPC security group rules](https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html)). **Customer responsibility for production**: add AWS Network Firewall FQDN rules or a forward proxy, plus Route 53 Resolver DNS Firewall; documented in HARDENING.md. |

---

## GenAI-specific threats

The LLM and its subprocess tooling introduce threats that do not fit cleanly under a single STRIDE letter. These are called out explicitly so reviewers can evaluate the mitigation strategy on its own terms.

| ID | Threat | Mitigation |
|----|--------|------------|
| AI-1 | Prompt injection: a crafted task description coerces the model into exfiltrating secrets, editing out-of-scope files, or chaining attacks against the git provider | Task descriptions are forwarded to Amazon Bedrock verbatim. Mitigations in layers: Bedrock's upstream safety filters on the selected model; Cedar policies scoped to `opencode___{tool}` action ARNs (so the model cannot reach tools it was not authorized for); microVM per-session isolation; credential scanner on pushed output. The OpenCode process holds the execution role's credentials, which are not scoped to the session's work directory (see OC-E). **Residual risk**: no dedicated prompt-injection filter (e.g. Amazon Bedrock Guardrails). Documented in HARDENING.md. **Customer responsibility**: layer a Bedrock Guardrail for production. |
| AI-1b | Prompt injection through repository content (README, source files, a `.opencode/` directory, or a repo-level `opencode.json`) rather than the task prompt | Same layers as AI-1. OpenCode config is passed inline via `OPENCODE_CONFIG_CONTENT`, which is expected to take precedence over a repository's own config on OpenCode 1.18.34; this is to be verified at deploy. **Residual risk**: repository content is untrusted model input, and the agent runs with the credentials described in OC-E. Root fix (user-bound workload tokens, a narrower agent role) is deferred to a design spike. |
| AI-2 | Output contains sensitive data from the source repo | Credential scanner runs between OpenCode output and `git push`. Covered patterns: AWS access keys, `sk-` API keys, GitHub tokens, GitLab PATs, PEM private keys, high-entropy `secret=`/`password=`/`token=`/`key=` assignments. **Residual risk**: formats outside the regex set pass through. **Customer responsibility**: extend patterns or add secondary scanning (e.g. GitGuardian, gitleaks) on GitHub. |
| AI-3 | Model outputs malicious code that compromises the reviewer's machine on clone | PRs land in the user's own repo; review is the user's responsibility. The credential scanner does not claim to detect malicious code. **Customer responsibility**: treat LLM-authored PRs the same as PRs from an external contributor: CI + human review before merge. |
| AI-4 | Customer data is used for model training or retained by AWS | Amazon Bedrock is pre-approved for this workload; the Anthropic Claude models on Bedrock do not train on customer prompts per the Bedrock service terms. Repository contents stay inside the per-session microVM and are discarded at session end (work directories are not on the managed session storage mount today; if they are moved there, they persist until the storage resets after 14 idle days or a runtime version update). |
| AI-5 | Third-party AI tool (OpenCode) is backdoored upstream | OpenCode is MIT-licensed. The Dockerfile pins version 1.18.34, downloads the `linux-arm64` release asset directly from the `anomalyco/opencode` GitHub release, and verifies it with `sha256sum -c` against a pinned checksum; a mismatch fails the build, and there is no `curl \| bash` installer. The container image is a CDK Docker image asset, rebuilt and pushed to the CDK bootstrap ECR repository on every `cdk deploy`. **Customer responsibility**: review upstream releases before bumping the version and checksum, and consider an internal mirror or provenance verification for production. |
| AI-6 | Biased or unsafe model outputs | The sample does not add bias/fairness controls beyond those provided by the upstream model. This is a code-generation agent, not a decision-making agent in a safety-critical domain. |

---

## Residual risks (accepted by design)

These are the risks the sample explicitly accepts because of its scope (it is a sample, not a production service). Each is either called out in HARDENING.md or flagged above.

1. **Cedar policies default to `LOG_ONLY`.** Production adopters are expected to flip to `ENFORCE` only after verifying decisions in `LOG_ONLY`, including that the role-gated permits produce ALLOW for users carrying `custom:role`; if the role does not reach the policies, every user is denied under `ENFORCE` (MC-E).
2. **Cognito MFA is not enforced.** Production adopters are expected to enable MFA.
3. **Outbound traffic is not FQDN-restricted beyond port 443, and DNS egress via the Route 53 Resolver is not filterable by security groups.** Production adopters are expected to add Network Firewall or a forward proxy, plus Route 53 Resolver DNS Firewall.
4. **NAT Gateway is single-AZ by default** (cost optimization). Production adopters are expected to scale to one NAT per AZ.
5. **No dedicated prompt-injection filter.** Production adopters are expected to layer a Bedrock Guardrail.
6. **Credential scanner is regex-based.** Production adopters are expected to extend patterns or add a secondary scanner.
7. **The job table has no cross-user index.** Operators have no built-in query for all RUNNING jobs; stale RUNNING rows left by a killed microVM must be found per user or via an export.
8. **No AWS Budget alert is created.** The `daily_cost_budget_usd` context value is a reference; production adopters create the budget out-of-band.
9. **AgentCore-managed secrets (`bedrock-agentcore-identity*`) use AWS-owned keys** by default. Customer-managed keys can be configured if the threat model requires them.
10. **OpenCode runs with the execution role's credentials**, not credentials scoped to one task (OC-E). The git-hardening controls (PL-T3) protect the integrity of the push and PR only; a `setsid`-detached descendant of the agent can outlive the process-group kill (RT-D).
11. **Repository content can carry prompt injection** into the agent (AI-1b).
12. **The OAuth callback is not bound to an authenticated browser session** (CB-S).
13. **The production-repo forbid is a case-sensitive suffix guardrail**, not an access boundary (CP-E).
14. **The GitHub OAuth `repo` scope grants read/write access to every repository the user can access** (see [HARDENING.md#github-oauth-scope](HARDENING.md#github-oauth-scope)).

## Out-of-scope threats

Explicitly not modelled here:

- AWS account takeover (we assume the account operator is trusted).
- Denial of service from a logged-in authenticated user (rate limiting is the customer's operational responsibility).
- Side-channel attacks across Firecracker microVMs (AWS platform responsibility).
- Physical/infrastructure attacks on AWS data centres (AWS platform responsibility).
- Client-side attacks on the MCP client itself (client vendor responsibility; the user's device is the trust root for the user actor).

## Review cadence

This threat model is reviewed when:

1. A new AWS service is added to the stack graph.
2. A new tool is added to the FastMCP server.
3. The credential scanner's regex set is changed.
4. The Cedar policy set is re-scoped.
5. `aws_cdk.aws_bedrock_agentcore_alpha` is upgraded to a stable module (or forked).

The maintainer is responsible for updating HARDENING.md and this document in the same change set when any of those conditions trigger.
