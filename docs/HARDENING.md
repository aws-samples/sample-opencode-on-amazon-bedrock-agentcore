<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Hardening

This is the production-hardening guide for the sample. It covers Amazon Virtual Private Cloud (Amazon VPC), Amazon Bedrock, Amazon Bedrock AgentCore, and AWS Key Management Service (AWS KMS) configuration choices that differ between a demo deployment and a production one. The defaults in the CDK stacks optimize for cost and simplicity so you can stand up a dev or demo deployment quickly. The notes below describe how to take that deployment closer to production-ready: highly available NAT, enforced Cedar policies, budget alerts, and the known limitations you should design around. Controls are listed from highest to lowest operational impact.

## NAT Gateway High Availability

The default `nat_gateways=1` in [`../stacks/vpc_stack.py`](../stacks/vpc_stack.py) is a **cost optimization for dev and sample workloads**. It routes all outbound traffic from private subnets through a single NAT Gateway in one Availability Zone.

For **production deployments**, set `nat_gateways` to match the number of AZs (default is 2, or the length of your `availability_zones` list). With a single NAT Gateway, an AZ failure takes out **all outbound connectivity** for the entire VPC, meaning the Runtime cannot reach Bedrock, GitHub, DynamoDB, or any other external service until the AZ recovers.

To change this, update the `nat_gateways` value in [`../stacks/vpc_stack.py`](../stacks/vpc_stack.py):

```python
# Production: one NAT Gateway per AZ for high availability
"nat_gateways": 2,  # match your AZ count
```

The tradeoff is cost: each NAT Gateway adds ~$32/month plus data transfer charges. For dev/test environments where brief outages are acceptable, the single NAT Gateway default keeps costs down.

## Cedar Policy Engine

The `OpenCodeGateway` stack ([`../stacks/gateway_stack.py`](../stacks/gateway_stack.py)) deploys the Cedar Policy Engine next to the Gateway and exposes it as the `PolicyEngineId` / `PolicyEngineArn` outputs. Cedar policies are created post-deploy via [`../scripts/create-policies.py`](../scripts/create-policies.py), which reads `PolicyEngineId` and `GatewayArn` from the `OpenCodeGateway` stack outputs, because the `CfnPolicy` CloudFormation resource handler has stabilization issues. The Gateway associates with the Policy Engine in **LOG_ONLY** mode by default, configured natively in CDK via `AWS::BedrockAgentCore::Gateway.PolicyEngineConfiguration`. In this mode, policy violations are logged but not blocked, so you can validate policy behavior before enforcing.

**Bundled policies.** `create-policies.py` creates six policies, four forbids then two role-gated permits:

| Policy | Effect |
|--------|--------|
| `opencode_readonly_deny_coding` | Forbid `run_coding_task` for the `readonly` role |
| `opencode_readonly_deny_cancel` | Forbid `cancel_task` for the `readonly` role |
| `opencode_readonly_deny_code` | Forbid `code` for the `readonly` role |
| `opencode_deny_production_repos` | Forbid `code` and `run_coding_task` when `repo_url` is `like` `*-production`, `*-production.git`, or `*-production/` |
| `opencode_permit_full_access` | Permit `AgentCore::OAuthUser` principals with role `admin` or `developer` on the six tools, listed explicitly |
| `opencode_permit_readonly_status` | Permit `AgentCore::OAuthUser` principals with role `readonly` on `get_task_status` and `list_tasks` |

Cedar is default-deny and a forbid always wins ([Cedar authorization](https://docs.cedarpolicy.com/auth/authorization.html)). Under ENFORCE, only the two permits let calls through: a caller with no role or an unrecognised role is denied every tool, and `readonly` is denied everything except `get_task_status` and `list_tasks` (including `connect_git_host`). The readonly forbids are kept as defence in depth. Assign every user a role before switching. Deployments that switch the Gateway to IAM inbound auth need their own permit for `AgentCore::IamEntity` principals ([AgentCore Policy principal types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html)). Re-running `create-policies.py` converges by name: it creates missing policies, updates changed statements in place, and skips identical ones. Policies in a FAILED state are deleted and recreated; no other policy is deleted.

The role comes from the `custom:role` claim in the Cognito ID token. The Gateway exposes JWT claims as principal tags keyed by claim name, so every policy reads the role with `principal.hasTag("custom:role") && principal.getTag("custom:role") == "<role>"` (`ROLE_TAG_KEY` in the script). Values are matched exactly (case-sensitive). There are no Cognito groups; `custom:role` is the only role mechanism. The app client can read `custom:role` but not write it, so roles are set only through Cognito admin APIs:

```bash
aws cognito-idp admin-update-user-attributes \
  --user-pool-id $USER_POOL_ID \
  --username user@example.com \
  --user-attributes Name=custom:role,Value=developer \
  --region $AWS_REGION
```

The production forbid is a case-sensitive match on the raw submitted `repo_url` string. Treat it as a guardrail, not an access boundary; the git provider's OAuth token is the access boundary.

**Verify in LOG_ONLY before switching.** If the role tag does not reach the policies (wrong attribute name, stale token, user with no role), every permit misses and default-deny rejects every call. The check therefore has to prove ALLOW decisions from the role-gated permits, not just DENY decisions:

1. Set `custom:role=readonly` on one test user with the command above. Set `Value=developer` on a second test user.
2. Get a fresh ID token for each user so it carries the new attribute.
3. As readonly, call `list_tasks` and expect ALLOW with `opencode_permit_readonly_status` determining the decision in the policy decision log.
4. As readonly, call `code` and expect DENY from `opencode_readonly_deny_code`.
5. As developer, call `list_tasks` and expect ALLOW with `opencode_permit_full_access` determining (only that permit covers `developer`, and `list_tasks` does not start a coding job).

If step 3 or step 5 shows DENY with no determining permit, the role is not reaching the policies and ENFORCE would deny every user. Do not switch until both show ALLOW.

**Switching from LOG_ONLY to ENFORCE mode:**

Once you've reviewed the CloudWatch logs and confirmed the policies match your intent, update the `PolicyEngineConfiguration` override in [`../stacks/gateway_stack.py`](../stacks/gateway_stack.py) from `"Mode": "LOG_ONLY"` to `"Mode": "ENFORCE"` and redeploy with `cdk deploy OpenCodeGateway`. `create-policies.py` never changes the mode.

**Adding custom policies:**

Use [`../scripts/create-policies.py`](../scripts/create-policies.py) as a template. Action names follow the `{target}___{tool}` format (e.g., `opencode___run_coding_task`), and the resource must reference the specific gateway ARN. Use `validationMode="IGNORE_ALL_FINDINGS"` for policies referencing tools discovered dynamically.

## Key Management Strategy

The sample provisions a single customer-managed AWS KMS key (CMK) in [`../stacks/security_stack.py`](../stacks/security_stack.py) and threads it through every stack that needs encryption at rest. Summary:

- **Key type:** Symmetric customer-managed CMK, one per deployment.
- **Rotation:** Automatic rotation is enabled (`enable_key_rotation=True`). AWS KMS rotates the key material annually; no action required on your part.
- **Key policy:** The default key policy permits the account root and grants use to the stack-created roles (Runtime execution role, Gateway role, Lambda roles). Review and tighten if you need to constrain which principals can use the key.
- **Alias:** `alias/opencode-cmk` for easy lookup.
- **Removal policy:** `RETAIN`, so `cdk destroy` does not delete the key. This prevents accidental loss of encrypted data in DynamoDB, CloudWatch Logs, Secrets Manager, or S3. [`../scripts/cleanup-retained-resources.sh`](../scripts/cleanup-retained-resources.sh) does not touch the key; schedule key deletion manually (`aws kms schedule-key-deletion`) when you're done with the sample.
- **Services using the CMK:** AWS Secrets Manager (the stack-created `opencode/webhook-signing-secret`), Amazon DynamoDB (job records), Amazon CloudWatch Logs (every log group the stacks create), Amazon S3 (CloudTrail bucket when enabled). The `opencode/github-oauth-app` secret written by `scripts/setup-oauth-app.sh` uses the AWS-managed Secrets Manager key; pass `--kms-key-id` in the script if you want it on the CMK. Amazon Bedrock AgentCore managed resources (Gateway, Runtime, Policy Engine, Identity Vault) are encrypted with AWS-owned keys by default; these can be switched to customer-managed keys via the relevant service-level configuration if your threat model requires it.

For a production deployment, consider:

1. Splitting the CMK into per-data-type keys (one for secrets, one for logs, one for DynamoDB) if you need separate key policies or rotation schedules.
2. Adding explicit condition keys (`kms:ViaService`, `kms:CallerAccount`) to the key policy.
3. Enabling AWS CloudTrail data events on the CMK for full key-usage auditing.

## Execution role wildcards

The Runtime execution role in [`../stacks/agentcore_stack.py`](../stacks/agentcore_stack.py) carries these wildcards, each explained in its cdk-nag `IAM5` suppression:

- CloudWatch Logs/Metrics `*`: service-required for `PutMetricData` and `DescribeLogGroups`; convenience for `CreateLogGroup`, `DescribeLogStreams`, `CreateLogStream`, `PutLogEvents`.
- X-Ray `*` and `ecr:GetAuthorizationToken` `*`: service-required.
- ECR `repository/*`: convenience (the CDK bootstrap asset repository name depends on the bootstrap qualifier).
- Bedrock `foundation-model` Region wildcard on the pinned model ID: required when `default_model_id` uses a geo/global inference-profile prefix, because the profile routes to the model in any eligible Region; convenience otherwise. DynamoDB access is pinned to `table/opencode-jobs` with no wildcard.
- AgentCore `arn:aws:bedrock-agentcore:<region>:<account>:*`: convenience.
- Secrets Manager `secret:bedrock-agentcore-identity*`: prefix-scoped.
- KMS `GenerateDataKey*` / `ReEncrypt*`: action wildcards on the one CMK.

There is no `sts:AssumeRole` statement and no per-task scoped credential. For production, narrow the convenience wildcards, following the [AgentCore reference execution-role policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html):

- `CreateLogGroup` / `DescribeLogStreams` to `log-group:/aws/bedrock-agentcore/runtimes/*`, and `CreateLogStream` / `PutLogEvents` to `log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`. Leave `DescribeLogGroups` on `log-group:*`.
- `PutMetricData` with a `cloudwatch:namespace` condition.
- AgentCore actions to the workload identity, token vault, and runtime ARNs. Keep `bedrock-agentcore:ListAgentRuntimes` on resource `*` (it is a list action with no resource-level scope); `cancel_task` uses it to discover the Runtime's own ARN by `RUNTIME_NAME`, unless you set `RUNTIME_ARN` explicitly on the Runtime after the first deploy.
- ECR to the bootstrap asset repository ARN.
- Where JWTs are available, deny `GetWorkloadAccessTokenForUserId` and use `GetWorkloadAccessTokenForJWT`, as AWS recommends. This sample still uses `ForUserId`; see [THREAT-MODEL.md](THREAT-MODEL.md) OC-E.

## AWS Budgets for Cost Control

The `daily_cost_budget_usd` value in `cdk.json` (default: `50`) is a **reference value only**. It is not enforced by the stack -- there is no AWS Budget, alarm, or throttle created automatically. If Bedrock costs exceed this amount, no default alert fires unless you set up monitoring yourself.

To catch runaway Bedrock costs, create an AWS Budget with daily notifications:

1. Open the [AWS Budgets console](https://console.aws.amazon.com/billing/home#/budgets) or use the CLI
2. Create a **Cost budget** scoped to the `Amazon Bedrock` service
3. Set the budget amount to your `daily_cost_budget_usd` value and the period to **Daily**
4. Add two alert thresholds:
   - **80% of budget** -- early warning that costs are trending high
   - **100% of budget** -- immediate notification that the daily limit has been reached
5. Configure an SNS topic or email as the notification target

Using the CLI:

```bash
aws budgets create-budget \
  --account-id $CDK_DEFAULT_ACCOUNT \
  --budget '{
    "BudgetName": "opencode-daily-bedrock",
    "BudgetLimit": {"Amount": "50", "Unit": "USD"},
    "TimeUnit": "DAILY",
    "BudgetType": "COST",
    "CostFilters": {"Service": ["Amazon Bedrock"]}
  }' \
  --notifications-with-subscribers '[
    {"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": 80, "ThresholdType": "PERCENTAGE"}, "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "your-email@example.com"}]},
    {"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": 100, "ThresholdType": "PERCENTAGE"}, "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "your-email@example.com"}]}
  ]'
```

For full setup options, see the [AWS Budgets documentation](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html).

## Known Limitations

- **Outbound traffic from the microVM is not FQDN-restricted.** The Runtime security group allows exactly TCP 443 to any IPv4 destination; AWS service traffic routes through VPC endpoints. Git clone and push traffic to any HTTPS host on the public internet is unfiltered via the NAT Gateway, and DNS queries to the Route 53 Resolver cannot be filtered by security groups ([VPC security group rules](https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html)). Git hosts on non-443 ports and SSH remotes are unsupported by design (repo URLs must be `https://`). For production, add AWS Network Firewall FQDN rules or a forward proxy, plus Route 53 Resolver DNS Firewall.
- **No cross-user view of jobs.** The `opencode-jobs` table ([`../stacks/job_store_stack.py`](../stacks/job_store_stack.py)) is partitioned by `user#{user_id}` with no secondary index, so `list_tasks` only ever returns the caller's own jobs and there is no built-in operator query for all RUNNING jobs. Add an index or an export if you need fleet-wide monitoring.
- **Amazon Cognito MFA is not enforced on the sample user pool.** The user pool is demo-scoped; you are responsible for enabling MFA ([Cognito MFA configuration](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-mfa.html)) and enforcing password policies suitable for your environment before routing real users through it.
- **No prompt-injection or output-content filter is applied to LLM I/O.** The pipeline relies on the upstream Amazon Bedrock model's built-in safety filters, a credential scanner ([`container/tools/scan_and_strip_credentials.py`](../container/tools/scan_and_strip_credentials.py)) that removes common credential patterns from pushed output, Cedar policies scoped to specific `opencode___{tool}` action ARNs, and microVM isolation per session. For stronger guarantees, layer on an [Amazon Bedrock Guardrail](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) and extend the credential scanner's regex set.
- **OpenCode runs with the execution role's credentials, and repository content can carry prompt injection.** The credentials are not scoped per task or per work directory. The git hardening in the pipeline (`.git/config` restore, hooks and credential helpers disabled, process-group kill) protects the integrity of the push and PR, not the credentials. See [THREAT-MODEL.md](THREAT-MODEL.md) OC-E, PL-T3, and AI-1b. The root fix (user-bound workload tokens, a narrower agent role) is deferred.
- **The OAuth callback is not bound to an authenticated browser session.** See [THREAT-MODEL.md](THREAT-MODEL.md) CB-S. Binding is deferred.

## GitHub OAuth scope

The sample requests the classic `repo` scope ([`../container/code_mcp_server.py`](../container/code_mcp_server.py), [`../container/tools/resolve_git_credential.py`](../container/tools/resolve_git_credential.py)). Per the [GitHub OAuth scopes reference](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps), `repo` grants full read/write access to code and other resources in every public and private repository the user can access, plus some organization-owned resources. A GitHub App with per-repository installation and fine-grained permissions is the better end state; it is not implemented in this sample.

## Third-party dependencies and AI components

This sample uses two third-party components at runtime, both referenced (not vendored) and fetched at container build time:

- **[OpenCode](https://opencode.ai)** - MIT-licensed AI coding agent, pinned to version 1.18.34 in [`../container/Dockerfile`](../container/Dockerfile) (`ARG OPENCODE_VERSION`). The build downloads the `opencode-linux-arm64.tar.gz` release asset directly from the `anomalyco/opencode` GitHub release and verifies it with `sha256sum -c` against a pinned checksum (`ARG OPENCODE_SHA256_ARM64`); a mismatch fails the build. There is no `curl | bash` installer. The image is built for `linux/arm64` only, so only the arm64 asset is pinned. Upstream source: https://github.com/anomalyco/opencode (formerly `sst/opencode`). Bumping `OPENCODE_VERSION` requires updating the checksum (it matches the `digest` field in the GitHub releases API).
- **[FastMCP](https://gofastmcp.com)** - MIT-licensed MCP server framework, installed from PyPI via [`../container/requirements.txt`](../container/requirements.txt).

The LLM itself is Amazon Bedrock-hosted Anthropic Claude, a pre-approved model available through the Amazon Bedrock marketplace. Bedrock enforces its own content filters and safety controls upstream of this sample; customer-side responsibility is limited to model access control via IAM (scoped to specific model ARNs in [`../stacks/agentcore_stack.py`](../stacks/agentcore_stack.py)) and application-level input/output sanitization.

The sample processes user-supplied git repositories as input to the LLM. Repositories are cloned into the per-session Firecracker microVM and fed to OpenCode. Repository contents are not written to CloudWatch Logs or DynamoDB by the sample, and the work directory is not on the managed session storage mount today (see [Other regions - managed session storage](#tested-regions)), so clones are discarded when the session ends. The DynamoDB job record stores the task description, repo URL, and branch names. Logs may contain repo URLs and git/OpenCode error text; OpenCode's stderr is streamed to the Runtime log at INFO while it runs, and on a non-zero exit a stderr tail is logged at ERROR and stored in the job record's `error` field. The credential scanner runs between LLM output and the git push to reduce the risk of secrets leaking into the PR; it covers files the agent committed itself (`base_sha..HEAD`) as well as staged, unstaged, and untracked files, and the PR contains a single squashed commit built from the scanned tree, so content removed by the scanner is not reachable from the pushed branch.

## Deployment Notes

### Tested regions

This sample has been tested and deploys successfully in:

- **us-east-1** (US East - N. Virginia)
- **eu-central-1** (Europe - Frankfurt)

**us-west-2 may have deployment issues.** The `AWS::BedrockAgentCore::GatewayTarget.CredentialProvider` schema in us-west-2 was previously a version behind (missing the `IamCredentialProvider` sub-type). This may have been resolved since last tested. AgentCore Gateway is available in 14 commercial regions as of the latest documentation; check the [AgentCore supported regions page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) for the current list. Deploy to us-east-1 or eu-central-1 for confirmed compatibility.

**Other regions - managed session storage.** `FilesystemConfigurations` on `AWS::BedrockAgentCore::Runtime` is documented in the CFN template reference but not yet accepted by the CFN schema validator in every region. [`../stacks/agentcore_stack.py`](../stacks/agentcore_stack.py) only emits the property (a `SessionStorage` mount at `/mnt/session`) in `us-east-1` (the only confirmed-deployable region where the Runtime schema also accepts it). In every other deployable region the mount is not configured, but everything else works. Note that the pipeline creates work directories under `SESSION_STORAGE_PATH`, which defaults to `/tmp/opencode-sessions` and is not set by the stack, so work directories are not on the `/mnt/session` mount in any region today; pointing it at the mount is a deferred follow-up. Override via CDK context `-c enable_filesystem_configurations=true` if your region's schema has since caught up (or `false` to turn it off in `us-east-1`). AWS documents managed session storage as Preview; session data resets after 14 days without invocation and when the runtime version is updated ([filesystem configurations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)).

### CloudWatch Transaction Search for AgentCore spans

AgentCore delivers Runtime and Gateway spans through CloudWatch Transaction Search, which the sample does not enable: it is an account-and-region setting (X-Ray trace segment destination `CloudWatchLogs`), not a stack resource. Until it is on, every span batch is rejected and the Runtime log shows `Failed to export span batch code: 400` at each cold start; the GenAI observability dashboard then has metrics but no traces. For production, enable Transaction Search per [Get started with AgentCore observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html). Spans are billed as CloudWatch Logs ingestion, and 1% of them is indexed as trace summaries at no extra charge by default ([Enable Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html), [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)); raise the indexing percentage only if you need more searchable traces. The companion `Failed to export logs batch code: 400 ... log stream does not exist` line is a cold-start race on the platform-named log stream and needs no action; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#otel-exporter-400-errors-at-cold-start).

### Gateway error detail

Detailed Gateway error output is off by default: no `ExceptionLevel` is set on the Gateway. To surface verbose errors to callers while debugging, redeploy with `-c gateway_exception_level=DEBUG` (case-insensitive; any other value leaves it off). Do not leave it on in production.

### Experimental CDK module

[`../stacks/gateway_stack.py`](../stacks/gateway_stack.py) depends on `aws_cdk.aws_bedrock_agentcore_alpha`, an alpha/experimental CDK module. The module is used for:

- `Gateway` - L2 construct for the AgentCore Gateway
- `CustomJwtAuthorizer` - Cognito JWT inbound authorization
- `GatewayExceptionLevel` - opt-in DEBUG exception level (`gateway_exception_level` context)
- `LambdaInterceptor` - REQUEST interceptor wiring
- `GatewayCredentialProvider.from_iam_role()` - GATEWAY_IAM_ROLE credential provider
- `Gateway.add_mcp_server_target()` - MCP target creation

Alpha APIs may break across minor version bumps. `requirements.txt` pins `aws-cdk.aws-bedrock-agentcore-alpha` with a tight upper bound (currently `>=2.251.0a0,<2.252.0a0`) so minor version bumps of the alpha module require a deliberate synth-and-diff review. Upgrade by bumping both the lower bound and the upper bound together, then running `cdk synth --all` to confirm the template is unchanged.

**Known alpha-module gap - `IamCredentialProvider` sub-object.** `GatewayCredentialProvider.from_iam_role()` emits only `{"CredentialProviderType": "GATEWAY_IAM_ROLE"}` in the synthesized template, omitting the sibling `CredentialProvider.IamCredentialProvider` sub-object that the CFN runtime handler requires. [`../stacks/gateway_stack.py`](../stacks/gateway_stack.py) works around this with an `add_property_override` escape hatch on the underlying `CfnGatewayTarget`. The override injects `{"IamCredentialProvider": {"Service": "bedrock-agentcore"}}` at the correct path. This works in regions whose CFN schema knows about `IamCredentialProvider` (confirmed in us-east-1 and eu-central-1). us-west-2 is blocked by a separate regional schema lag - see the Tested regions section.

**Known alpha-module gap - Gateway -> DefaultPolicy ordering.** The alpha `Gateway` L2 attaches IAM permissions (including `bedrock-agentcore:GetPolicyEngine`) via `add_to_principal_policy`, which CDK synthesizes into a `DefaultPolicy` resource that is a sibling of the Gateway in the template. When the Gateway resource carries a `PolicyEngineConfiguration` property, the CFN handler validates the policy-engine reference by calling `GetPolicyEngine` using the Gateway's role at creation time - which races the DefaultPolicy attachment and fails with `AccessDenied`. [`../stacks/gateway_stack.py`](../stacks/gateway_stack.py) adds explicit `cfn_gateway.add_dependency(...)` edges on both the in-stack `CfnPolicyEngine` and the role's `DefaultPolicy` to force the correct ordering.

**Fallback path:** if the alpha L2 drifts, the L1 `aws_cdk.aws_bedrockagentcore.CfnGatewayTarget` with `McpTargetConfigurationProperty` is the documented alternative. The `PolicyEngineConfiguration` is already attached via an `add_property_override` escape hatch on the underlying `CfnGateway`, so it is unaffected by alpha-module drift.

### Why `create-policies.py` is still a script

`AWS::BedrockAgentCore::Policy` (the `CfnPolicy` resource) has a service-side stabilization issue: the CloudFormation resource handler reports `NotStabilized` / `Resource stabilization failed` even when policy creation succeeds, causing stack `CREATE_FAILED` and rollback. [`../scripts/create-policies.py`](../scripts/create-policies.py) bypasses CloudFormation entirely: it reads `PolicyEngineId` and `GatewayArn` from the `OpenCodeGateway` stack outputs (its only input is `--region`), polls `get_policy` for up to 60 seconds per policy, and cleans up `FAILED` leftovers from previous attempts.

**Unblock criterion:** this script can be migrated into CDK when AWS ships the service-side fix to `CfnPolicy` stabilization (tracked via the AWS "What's New" feed for AgentCore Policy).

**Note:** `AWS::BedrockAgentCore::Gateway.PolicyEngineConfiguration` does **not** share this stabilization bug and is attached natively in CDK via an `add_property_override` escape hatch on the underlying `CfnGateway`.
