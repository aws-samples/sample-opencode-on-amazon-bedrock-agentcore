<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Troubleshooting

Common problems seen during deploy, redeploy, and cleanup, and how to get past them.

## "Resource already exists" errors after a previous deployment

Several resources use the `RETAIN` removal policy (the `opencode-jobs` DynamoDB table, the KMS CMK, the Cognito user pool, CloudWatch log groups, and the CloudTrail bucket when enabled) to prevent accidental data loss. After `cdk destroy` they remain; the fixed-name `opencode-jobs` table is the one that causes an "already exists" error on the next `cdk deploy`. Run the cleanup script before redeploying:

```bash
export AWS_REGION=us-east-1   # match your target region
./scripts/cleanup-retained-resources.sh
```

The script removes: the `opencode-jobs` DynamoDB table and any orphaned security groups, subnets, and VPCs tagged with `Project=OpenCode`. It leaves the retained log groups (CDK-generated names, so they do not collide), the CMK, and the Cognito user pool in place; delete those by hand if you want them gone. There is no sample-owned S3 bucket or ECR repository to clean up: the container image lives in the CDK bootstrap asset repository.

AgentCore-managed ENIs attached to security groups can persist for up to 8 hours after runtime deletion (per the [AgentCore VPC docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)). If the script reports "ENIs may still be releasing", run it again later. The SGs and VPC are orphaned but won't block a fresh deploy - CDK creates new ones.

## IAM role already exists during deployment

If deploying to a second region in the same account, IAM roles (which are global) may conflict. The role names include the region suffix (e.g., `opencode-agentcore-execution-role-us-east-1`) to prevent this. If you see this error from an older deployment, delete the orphaned role manually.

## Security group deletion fails during `cdk destroy`

AgentCore runtimes create ENIs in your VPC subnets that are managed by the service (`InterfaceType=agentic_ai`). After the runtime is deleted, these ENIs can persist for up to 8 hours and cannot be detached by you (`OperationNotPermitted` on the `ela-attach-*` attachment). `cdk destroy` fails with `resource has a dependent object` on the `OpenCodeAgentCore` security group, and then on the `OpenCodeVpc` private subnets and VPC. Either wait for the ENIs to release and run `cdk destroy` again, or finish the destroy now by retaining the blocked resources and sweeping them later:

```bash
aws cloudformation delete-stack --stack-name OpenCodeAgentCore \
  --retain-resources AgentCoreSecurityGroup922A1612
aws cloudformation delete-stack --stack-name OpenCodeVpc \
  --retain-resources VpcPrivateSubnet1Subnet536B997A VpcPrivateSubnet2Subnet3788AAA1 Vpc8378EB38
# later, once the ENIs are gone:
./scripts/cleanup-retained-resources.sh
```

`--retain-resources` only accepts resources that are currently in `DELETE_FAILED`; check `describe-stack-events` for the exact logical IDs.

## `OpenCodeGateway` stuck in DELETE_FAILED: "Policy engine still contains N policies"

The Cedar policies are created by `scripts/create-policies.py`, not by CloudFormation, so they are still attached when `cdk destroy` tries to delete the `CfnPolicyEngine` in the `OpenCodeGateway` stack, and the delete fails with a `ConflictException`. Remove the policies first, then destroy:

```bash
python scripts/create-policies.py --delete --region $AWS_REGION
cdk destroy --all
```

The `--delete` flag only removes the six policies the script manages (matched by name) and is safe to re-run.

## CDK bootstrap required

Run `cdk bootstrap aws://<account>/<region>` before the first deployment to a new region.

## GitHub OAuth App not working

Verify the callback URL in your GitHub OAuth App matches the provider-specific URL from AgentCore Identity. Run `./scripts/setup-oauth-app.sh` — it displays the correct callback URL after registering the provider. The URL format is `https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<provider-uuid>`, where the UUID is assigned when the credential provider is created.

## `connect_git_host` says no credential provider is registered

The GitHub credential provider is not a CDK resource. Run `./scripts/setup-oauth-app.sh` after `cdk deploy` to create the `github-provider` OAuth2 credential provider in AgentCore Identity; until then every `connect_git_host` call returns `failed` and coding tasks fail with `git_host_not_connected`.

## Cedar denies every call after switching to ENFORCE

The permits only match users whose Cognito ID token carries `custom:role` set to `admin`, `developer`, or `readonly`. Set the attribute with `aws cognito-idp admin-update-user-attributes --user-attributes Name=custom:role,Value=developer` and get a fresh token. Verify ALLOW decisions in LOG_ONLY first; see [HARDENING.md](HARDENING.md#cedar-policy-engine). `create-policies.py` reads `PolicyEngineId` and `GatewayArn` from the `OpenCodeGateway` stack outputs, so it must run after that stack deploys.

## Gateway targets not working

The Gateway MCP Server target (`opencode`) is created natively in CDK via `Gateway.add_mcp_server_target()` and uses `GATEWAY_IAM_ROLE` for Gateway to Runtime authentication (SigV4). Tools are discovered dynamically via implicit sync. If the target is missing or misconfigured, re-run `cdk deploy OpenCodeGateway` to recreate it from the CloudFormation template.

## Gateway returns generic errors

Detailed Gateway error output is off by default. While debugging, redeploy with `cdk deploy OpenCodeGateway -c gateway_exception_level=DEBUG` to surface detailed errors to the caller, then redeploy without the flag when you're done.

## OTEL exporter 400 errors at cold start

Every microVM cold start writes two kinds of lines to the Runtime log group (`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`):

```
Failed to export span batch code: 400, reason: Bad Request
Failed to export logs batch code: 400, reason: ...The specified log stream does not exist.
```

The first comes from the OTLP HTTP trace exporter (`opentelemetry.exporter.otlp.proto.http.trace_exporter`), the second from the ADOT log exporter (`amazon.opentelemetry.distro...otlp_aws_log_record_exporter`). Neither is caused by repository code: no exporter configuration lives in this repo, the platform injects `AGENT_OBSERVABILITY_ENABLED` and the OTLP endpoint headers.

- **Spans 400.** AgentCore spans are delivered through CloudWatch Transaction Search, which requires the account's X-Ray trace segment destination to be `CloudWatchLogs`. If `aws xray get-trace-segment-destination` reports `Destination: XRay`, every span batch is rejected with 400. Remedy: enable Transaction Search in the account and region, see [Get started with AgentCore observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html). This is an operator step, not a code change; see [HARDENING.md](HARDENING.md#cloudwatch-transaction-search-for-agentcore-spans) for the cost note.
- **Logs 400.** The platform-named OTLP log stream does not exist yet when the container flushes its first batch. The first telemetry batch is lost; later batches succeed. Application logs written to stdout are unaffected. Benign, nothing to fix.

## `cancel_task` returns `cancel_failed`

`cancel_task` only writes `CANCELLED` when it actually stopped something. When it could not, it returns `{"job_id": ..., "status": <current record status, usually "RUNNING">, "error": "cancel_failed", "detail": ...}` and leaves the record alone. The `detail` tells you why:

| `detail` | Cause | What to do |
|----------|-------|------------|
| `job record has no runtime_session_id; cannot locate the microVM running it` | The record predates the session-id capture (older deployments) or the request reached the Runtime without a `baggage` header. | Nothing can be stopped remotely. Wait for the job to finish or time out (`timeout_minutes`, default 10); if the row stays `RUNNING` it is stale and can be deleted from `opencode-jobs`. |
| `runtime ARN unresolved` | The Runtime could not determine its own ARN (`RUNTIME_ARN` unset and `ListAgentRuntimes` discovery by `RUNTIME_NAME` failed or is not permitted). | Check the execution role has `bedrock-agentcore:ListAgentRuntimes` and that `RUNTIME_NAME` matches the Runtime name; redeploy `OpenCodeAgentCore`. |
| `StopRuntimeSession failed: <code>: <message>` | The control-plane call was rejected (permissions, throttling, wrong region). | Check the execution role's `bedrock-agentcore:StopRuntimeSession` permission and the Runtime log for the WARNING line. |
| `job reached a terminal state before the cancellation was recorded` | The job finished (`COMPLETE` or `FAILED`) between the stop and the `CANCELLED` write. | Nothing to do; `status` in the response is the final state. |

If the stopped microVM's own pipeline wins the race and records `CANCELLED` first (its `CancelledError` handler runs during shutdown), `cancel_task` still returns the success shape, with `detail` = `CANCELLED was recorded by the job's own cancellation handler`; the record then carries `error` = `Task cancelled` rather than `Task cancelled by user`.

A `detail` prefixed with `in-process cancel signalled but the task did not finish within 10s` means the job ran on the same microVM, the asyncio task was cancelled, but it did not unwind in time (the tool then fell through to `StopRuntimeSession`). `{"error": "Job is already in terminal state: <STATUS>"}` is not a failure: the job was already over when you called.

## `files_edited` is empty although the PR changes files

`files_edited` is parsed from OpenCode's ACP `tool_call` / `tool_call_update` notifications and lists only files written by edit-kind tools (paths relative to the repository root). The parser follows the payload shapes of the pinned OpenCode release (1.18.x: `locations[].path`, `rawInput.filePath`, `kind: "edit"`). If you bump `OPENCODE_VERSION` in `container/Dockerfile` and `files_edited` goes back to `[]` for jobs that clearly changed files, compare the new ACP payloads against the fixtures in `tests/unit/test_run_opencode_acp.py` and update `_collect_edited_paths` in `container/tools/run_opencode_acp.py`. Job records written before the parser fix keep `files_edited: []`; the PR diff is the source of truth either way.

## Regional deployment failures

If deployment fails with an unrecognized `AWS::BedrockAgentCore::*` resource type, the target region does not yet support Bedrock AgentCore. Deploy to a supported region (us-east-1 or eu-central-1 are confirmed working) or see the tested regions note in [HARDENING.md](HARDENING.md#tested-regions).
