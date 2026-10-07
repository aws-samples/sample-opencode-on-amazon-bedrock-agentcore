# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OpenCode AgentCore stack — execution role, security group, Runtime, Endpoint.

Bedrock IAM scoped to single default_model_id. Identity SDK permissions included.
Single FastMCP Python server on port 8000. Managed session storage enabled.
The container image is a CDK DockerImageAsset (CDK bootstrap ECR repository).

Requirements: 6.1, 6.4, 10.3, 14.1, 14.2, 14.3, 14.4
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as bedrockagentcore,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_kms as kms,
)
import cdk_nag
from constructs import Construct


class AgentCoreStack(cdk.Stack):
    """AgentCore base resources: IAM role, SG, Runtime, Endpoint."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        cmk: kms.IKey,
        callback_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._vpc = vpc
        self._cmk = cmk

        default_model_id = self.node.try_get_context("default_model_id") or "global.anthropic.claude-opus-4-6-v1"

        # -----------------------------------------------------------------
        # Security Group
        #
        # No ingress rules. Egress is a single rule: TCP 443 to 0.0.0.0/0
        # (IPv4 only; the VPC has no IPv6 CIDR). Port 443 serves:
        # - interface VPC endpoints inside the VPC (Bedrock, AgentCore,
        #   ECR, CloudWatch Logs/Monitoring, X-Ray, KMS, Secrets Manager, ...)
        # - the S3 and DynamoDB gateway endpoints (ECR image layers, managed
        #   session storage sync in VPC mode, the job table)
        # - public git hosts through the NAT Gateway (repo URLs are
        #   restricted to https:// in container/pipeline.py)
        # Execution-role credentials come from the AgentCore MicroVM Metadata
        # Service, independent of network mode. Security groups cannot block
        # DNS to the Route 53 Resolver, so no port 53 rule is needed.
        #
        # 443-only egress does NOT prevent data exfiltration to arbitrary
        # HTTPS hosts, and DNS queries to the Route 53 Resolver are not
        # filtered by this SG either. FQDN-level egress filtering is a
        # documented residual risk (docs/HARDENING.md#known-limitations);
        # production deployments should add AWS Network Firewall FQDN rules
        # or a forward proxy, plus Route 53 Resolver DNS Firewall.
        # -----------------------------------------------------------------
        self.agentcore_sg = ec2.SecurityGroup(
            self,
            "AgentCoreSecurityGroup",
            vpc=self._vpc,
            description="AgentCore container security group",
            allow_all_outbound=False,
        )
        self.agentcore_sg.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS to VPC endpoints, S3/DynamoDB gateway endpoints, and git hosts via NAT",
        )

        # -----------------------------------------------------------------
        # AgentCore Execution IAM Role
        # Bedrock scoped to single model. Identity SDK permissions included.
        # -----------------------------------------------------------------
        self.execution_role = iam.Role(
            self,
            "AgentCoreExecutionRole",
            role_name=f"opencode-agentcore-execution-role-{self.region}",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com",
                    conditions={
                        "StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                    },
                ),
            ),
            description="Execution role for OpenCode AgentCore containers",
        )

        # Bedrock InvokeModel — scoped to cross-region inference profile + its underlying
        # foundation model. When OpenCode calls the ``global.`` inference profile, Bedrock
        # fans out to the foundation model in each eligible region; both ARNs must be in
        # the allow list.
        bedrock_resources = []
        if default_model_id.startswith("arn:"):
            bedrock_resources.append(default_model_id)
        else:
            # Strip any region/global/us/eu prefix to derive the base foundation model id.
            # e.g. "global.anthropic.claude-opus-4-6-v1" → "anthropic.claude-opus-4-6-v1"
            _prefixes = ("global.", "us.", "eu.", "jp.", "apac.", "au.")
            base_model_id = default_model_id
            for _p in _prefixes:
                if base_model_id.startswith(_p):
                    base_model_id = base_model_id[len(_p):]
                    break
            # foundation-model ARN for the underlying model (no region prefix, no account)
            bedrock_resources.append(
                f"arn:aws:bedrock:*::foundation-model/{base_model_id}"
            )
            # inference-profile ARN for the cross-region profile (if the id has a prefix)
            if base_model_id != default_model_id:
                bedrock_resources.append(
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{default_model_id}"
                )

        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvokeModel",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_resources,
            )
        )

        # DynamoDB read/write for job store only (no team config table)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDbAccess",
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/opencode-jobs",
                ],
            )
        )

        # CloudWatch Logs and Metrics
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsAndMetrics",
                actions=[
                    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                    "logs:DescribeLogStreams", "logs:DescribeLogGroups",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # ECR image pull
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRImageAccess",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRTokenAccess",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # X-Ray tracing
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="XRayTracing",
                actions=[
                    "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules", "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )

        # AgentCore Identity SDK — credential management + cross-session cancellation
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreIdentity",
                actions=[
                    "bedrock-agentcore:GetCredential",
                    "bedrock-agentcore:ListCredentialProviders",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    "bedrock-agentcore:StopRuntimeSession",
                    # cancel_task discovers this runtime's own ARN by name
                    # (CloudFormation cannot inject a resource's own ARN).
                    "bedrock-agentcore:ListAgentRuntimes",
                ],
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"],
            )
        )

        # Secrets Manager read for Identity token vault (stores user OAuth tokens)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentityTokenVaultAccess",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity*",
                ],
            )
        )

        # KMS decrypt for CMK
        self._cmk.grant_encrypt_decrypt(self.execution_role)

        # -----------------------------------------------------------------
        # Container Image — ARM64, Python FastMCP server
        # -----------------------------------------------------------------
        self.image_asset = ecr_assets.DockerImageAsset(
            self,
            "OpenCodeImage",
            directory="container",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        container_uri = self.image_asset.image_uri

        # -----------------------------------------------------------------
        # AgentCore Runtime
        # -----------------------------------------------------------------
        private_subnet_ids = [
            subnet.subnet_id for subnet in self._vpc.private_subnets
        ]

        self.runtime = bedrockagentcore.CfnRuntime(
            self,
            "OpenCodeRuntime",
            agent_runtime_name="opencode_runtime",
            protocol_configuration="MCP",
            agent_runtime_artifact=bedrockagentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=bedrockagentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=container_uri,
                ),
            ),
            role_arn=self.execution_role.role_arn,
            network_configuration=bedrockagentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=bedrockagentcore.CfnRuntime.VpcConfigProperty(
                    subnets=private_subnet_ids,
                    security_groups=[self.agentcore_sg.security_group_id],
                ),
            ),
            description="OpenCode AgentCore Runtime — Python FastMCP server on port 8000",
        )

        # cancel_task needs this runtime's ARN for cross-session StopRuntimeSession calls.
        # CloudFormation does not allow self-referencing a resource's own attributes in its
        # properties and the platform does not inject the ARN, so the container discovers
        # it on first use via ListAgentRuntimes filtered by RUNTIME_NAME (see
        # _discover_runtime_arn_by_name). RUNTIME_ARN_PREFIX is kept for the
        # RUNTIME_ARN_PREFIX + AGENT_RUNTIME_ID shortcut when an operator sets the ID.
        self.runtime.add_property_override("EnvironmentVariables", {
            "RUNTIME_ARN_PREFIX": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/",
            "RUNTIME_NAME": "opencode_runtime",
            "WORKLOAD_NAME": "opencode_runtime",
            "OAUTH_CALLBACK_URL": callback_url,
            "AWS_REGION": self.region,
            "AWS_ACCOUNT_ID": self.account,
            "OPENCODE_MODEL": default_model_id,
            # EXPERIMENT 1: keep only AUTOUPDATE disabled (every cold start is
            # a fresh microVM — autoupdate would try to download a new binary
            # every time). All other DISABLE_* flags were added speculatively.
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
        })

        # Managed session storage — persists work directories across microVM stop/resume.
        # Uses escape hatch because FilesystemConfigurations is not yet in the CDK L1.
        #
        # Skip in regions whose CFN schema has not been updated yet (e.g.
        # eu-central-1). In those regions the session storage feature is
        # disabled — work directories won't persist across microVM
        # stop/resume, but everything else works. Override via cdk context
        # ``enable_filesystem_configurations=true|false`` to force behavior.
        _regions_with_fs_support = {"us-east-1"}
        _override = self.node.try_get_context("enable_filesystem_configurations")
        if _override is not None:
            _enable_fs = str(_override).lower() == "true"
        else:
            _enable_fs = self.region in _regions_with_fs_support

        if _enable_fs:
            self.runtime.add_property_override("FilesystemConfigurations", [
                {
                    "SessionStorage": {
                        "MountPath": "/mnt/session",
                    },
                },
            ])

        # -----------------------------------------------------------------
        # AgentCore Runtime Endpoint
        #
        # Important: agent_runtime_version must track the current runtime
        # version, otherwise the endpoint stays pinned to the initial version
        # (1) and every ``UpdateAgentRuntime`` creates a new version that the
        # endpoint ignores. See
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html
        # -----------------------------------------------------------------
        self.runtime_endpoint = bedrockagentcore.CfnRuntimeEndpoint(
            self,
            "OpenCodeRuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            agent_runtime_version=self.runtime.attr_agent_runtime_version,
            name="opencode_endpoint",
            description="OpenCode AgentCore Runtime Endpoint",
        )
        self.runtime_endpoint.add_dependency(self.runtime)

        # -----------------------------------------------------------------
        # Outputs
        # -----------------------------------------------------------------
        cdk.CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        cdk.CfnOutput(self, "RuntimeEndpointId", value=self.runtime_endpoint.ref)

        # -----------------------------------------------------------------
        # cdk-nag suppressions
        # -----------------------------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [cdk_nag.NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "Runtime execution role. Resource '*' is service-required "
                    "for cloudwatch:PutMetricData, the logs:* actions, X-Ray "
                    "PutTraceSegments/PutTelemetryRecords/GetSampling* and "
                    "ecr:GetAuthorizationToken, which have no resource-level "
                    "permissions. The remaining wildcards are prefix-scoped to "
                    "resources this sample owns: repository/* in this account "
                    "and Region (the CDK bootstrap container-assets repository "
                    "that holds the DockerImageAsset), "
                    "arn:aws:bedrock-agentcore:<region>:<account>:* for the "
                    "Identity SDK, StopRuntimeSession and ListAgentRuntimes "
                    "(a list action used by cancel_task to discover this "
                    "runtime's own ARN by name), and "
                    "secret:bedrock-agentcore-identity* for the Identity token "
                    "vault. The Region wildcard on the pinned foundation-model "
                    "ARN is required because the cross-Region inference profile "
                    "routes to the model in any eligible Region. The kms:* "
                    "action wildcards come from Key.grant_encrypt_decrypt() on "
                    "the single CMK. See docs/THREAT-MODEL.md 'Runtime "
                    "execution role'."
                ),
            )],
            apply_to_children=True,
        )

        # No cdk-nag suppressions on self.agentcore_sg: AwsSolutions-EC23
        # checks ingress only, and this SG has no ingress rules, so neither
        # EC23 nor CdkNagValidationFailure fires on it. The egress caveats
        # (443 does not stop HTTPS or DNS exfiltration) are documented on the
        # SG definition above. tests/unit/test_agentcore_stack.py
        # (test_no_unsuppressed_cdk_nag_errors) pins this.
