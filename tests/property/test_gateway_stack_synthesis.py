# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: GatewayStack CloudFormation synthesis.

Feature: 15-cdk-native-gateway-target

These Hypothesis-driven properties pin the synthesized CloudFormation
template for ``OpenCodeGateway`` after the MCP ``GatewayTarget`` and
``PolicyEngineConfiguration`` migrate from a post-deploy boto3 script
into CDK.

The shared ``_build_stacks`` helper builds a fresh ``cdk.App`` with a
stub AgentCore stack (exposing ``runtime`` as a ``CfnRuntime``) and the
real ``GatewayStack`` (which owns the Cedar policy engine) so each
property draw synthesizes end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_bedrockagentcore as bedrockagentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_kms as kms
from constructs import Construct
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stacks.gateway_stack import GatewayStack

# ---------------------------------------------------------------------------
# Context loading — match what cdk.json exposes at synth time
# ---------------------------------------------------------------------------

_CDK_JSON_PATH = Path(__file__).resolve().parents[2] / "cdk.json"


def _load_cdk_context() -> dict:
    with open(_CDK_JSON_PATH) as f:
        return json.load(f)["context"]


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_REGIONS = [
    "us-east-1",
    "us-east-1",
    "eu-west-1",
    "eu-central-1",
    "ap-northeast-1",
]

region_strategy = st.sampled_from(_REGIONS)
account_id_strategy = st.from_regex(r"[0-9]{12}", fullmatch=True)
runtime_id_strategy = st.from_regex(r"[A-Z0-9]{10}", fullmatch=True)


# ---------------------------------------------------------------------------
# Stub stacks
# ---------------------------------------------------------------------------


class _StubAgentCoreStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.runtime = bedrockagentcore.CfnRuntime(
            self,
            "StubRuntime",
            agent_runtime_name=f"stub_runtime_{runtime_id.lower()}",
            protocol_configuration="MCP",
            agent_runtime_artifact=bedrockagentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=bedrockagentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=(
                        f"123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                        f"opencode:{runtime_id.lower()}"
                    ),
                ),
            ),
            role_arn="arn:aws:iam::123456789012:role/stub-execution-role",
            network_configuration=bedrockagentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
        )


# ---------------------------------------------------------------------------
# Stack factory
# ---------------------------------------------------------------------------


def _build_stacks(
    *,
    region: str,
    account: str,
    runtime_id: str,
) -> tuple[cdk.App, GatewayStack, _StubAgentCoreStack]:
    ctx = _load_cdk_context()
    app = cdk.App(context=ctx)
    env = cdk.Environment(account=account, region=region)

    agentcore_stack = _StubAgentCoreStack(
        app, "StubAgentCore", runtime_id=runtime_id, env=env,
    )
    helper_stack = cdk.Stack(app, "HelperStack", env=env)
    user_pool = cognito.UserPool.from_user_pool_id(
        helper_stack, "StubUserPool", f"{region}_abcdefghi",
    )

    cmk_stack = cdk.Stack(app, "StubCmkStack", env=env)
    stub_cmk = kms.Key(cmk_stack, "StubCmk")

    gateway_stack = GatewayStack(
        app,
        "OpenCodeGateway",
        cognito_user_pool=user_pool,
        cognito_client_id="abcdefghijklmnopqrstuvwxyz",
        opencode_runtime=agentcore_stack.runtime,
        cmk=stub_cmk,
        env=env,
    )
    gateway_stack.add_dependency(agentcore_stack)

    return app, gateway_stack, agentcore_stack


# ---------------------------------------------------------------------------
# Property 1: exactly one MCP GatewayTarget with IAM credential provider
# ---------------------------------------------------------------------------


class TestMcpGatewayTargetProperties:
    """Property 1: exactly one MCP GatewayTarget with IAM credential provider."""

    @given(
        region=region_strategy,
        account=account_id_strategy,
        runtime_id=runtime_id_strategy,
    )
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_exactly_one_iam_mcp_target(
        self,
        region: str,
        account: str,
        runtime_id: str,
    ) -> None:
        _app, gateway_stack, _ac = _build_stacks(
            region=region,
            account=account,
            runtime_id=runtime_id,
        )

        template = assertions.Template.from_stack(gateway_stack)
        template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 1)

        tpl = template.to_json()
        targets = {
            lid: res
            for lid, res in tpl["Resources"].items()
            if res["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
        }
        assert len(targets) == 1
        _lid, target = next(iter(targets.items()))
        props = target.get("Properties", {})

        endpoint = (
            props.get("TargetConfiguration", {})
            .get("Mcp", {})
            .get("McpServer", {})
            .get("Endpoint")
        )
        assert endpoint not in (None, "", {})

        cred_configs = props.get("CredentialProviderConfigurations", [])
        assert len(cred_configs) >= 1
        first = cred_configs[0]
        assert first.get("CredentialProviderType") == "GATEWAY_IAM_ROLE"


# ---------------------------------------------------------------------------
# Property 2: PolicyEngineConfiguration attached with LOG_ONLY
# ---------------------------------------------------------------------------


class TestPolicyEngineConfigurationProperties:
    """Property 2: PolicyEngineConfiguration attached with LOG_ONLY."""

    @given(
        region=region_strategy,
        account=account_id_strategy,
        runtime_id=runtime_id_strategy,
    )
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_policy_engine_log_only_on_gateway(
        self,
        region: str,
        account: str,
        runtime_id: str,
    ) -> None:
        _app, gateway_stack, _ac = _build_stacks(
            region=region,
            account=account,
            runtime_id=runtime_id,
        )

        template = assertions.Template.from_stack(gateway_stack)
        template.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)

        tpl = template.to_json()
        gateways = {
            lid: res
            for lid, res in tpl["Resources"].items()
            if res["Type"] == "AWS::BedrockAgentCore::Gateway"
        }
        assert len(gateways) == 1
        _lid, gateway = next(iter(gateways.items()))
        props = gateway.get("Properties", {})

        pe_config = props.get("PolicyEngineConfiguration")
        assert pe_config is not None
        assert pe_config.get("Mode") == "LOG_ONLY"

        # The ARN is a GetAtt on the policy engine defined in the same stack.
        arn = pe_config.get("Arn")
        assert isinstance(arn, dict) and "Fn::GetAtt" in arn
        engine_lid, attr = arn["Fn::GetAtt"]
        assert attr == "PolicyEngineArn"
        assert tpl["Resources"][engine_lid]["Type"] == "AWS::BedrockAgentCore::PolicyEngine"


# ---------------------------------------------------------------------------
# Property 3: synthesis is idempotent for logical IDs
# ---------------------------------------------------------------------------


def _collect_logical_ids(template_json: dict, resource_type: str) -> list[str]:
    return sorted(
        lid
        for lid, res in template_json.get("Resources", {}).items()
        if res.get("Type") == resource_type
    )


class TestSynthesisIdempotenceProperties:
    """Property 3: idempotent logical IDs across successive synths."""

    @given(
        region=region_strategy,
        account=account_id_strategy,
        runtime_id=runtime_id_strategy,
    )
    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_logical_ids_are_stable_across_synths(
        self,
        region: str,
        account: str,
        runtime_id: str,
    ) -> None:
        _app1, gs1, _ac1 = _build_stacks(
            region=region, account=account, runtime_id=runtime_id,
        )
        gw_tpl_1 = assertions.Template.from_stack(gs1).to_json()

        _app2, gs2, _ac2 = _build_stacks(
            region=region, account=account, runtime_id=runtime_id,
        )
        gw_tpl_2 = assertions.Template.from_stack(gs2).to_json()

        for resource_type in (
            "AWS::BedrockAgentCore::Gateway",
            "AWS::BedrockAgentCore::GatewayTarget",
            "AWS::BedrockAgentCore::PolicyEngine",
        ):
            assert _collect_logical_ids(gw_tpl_1, resource_type) == \
                   _collect_logical_ids(gw_tpl_2, resource_type)


# ---------------------------------------------------------------------------
# Property 4: MCP endpoint URL shape
# ---------------------------------------------------------------------------


_ENDPOINT_REGEX = (
    r"https://bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com/runtimes/"
    r"arn%3Aaws%3Abedrock-agentcore%3A[a-z0-9-]+%3A[0-9]+"
    r"%3Aruntime%2F[A-Z0-9_-]+/invocations"
)


def _resolve_endpoint(
    value: object,
    *,
    region: str,
    account: str,
    runtime_id: str,
) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "Ref" in value:
            ref = value["Ref"]
            if ref == "AWS::Region":
                return region
            if ref == "AWS::AccountId":
                return account
            return f"<Ref:{ref}>"
        if "Fn::ImportValue" in value:
            export_name = value["Fn::ImportValue"]
            if isinstance(export_name, str) and "AgentRuntimeId" in export_name:
                return runtime_id
            return f"<ImportValue:{export_name!r}>"
        if "Fn::GetAtt" in value:
            parts = value["Fn::GetAtt"]
            if isinstance(parts, list) and len(parts) == 2 and parts[1] == "AgentRuntimeId":
                return runtime_id
            return f"<GetAtt:{parts!r}>"
        if "Fn::Join" in value:
            sep, items = value["Fn::Join"]
            return sep.join(
                _resolve_endpoint(item, region=region, account=account, runtime_id=runtime_id)
                for item in items
            )
        return f"<Intrinsic:{sorted(value.keys())!r}>"
    return f"<Unsupported:{type(value).__name__}>"


class TestMcpEndpointUrlShapeProperties:
    """Property 4: MCP endpoint URL is well-formed after token resolution."""

    @given(
        region=region_strategy,
        account=account_id_strategy,
        runtime_id=runtime_id_strategy,
    )
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_endpoint_url_shape(
        self,
        region: str,
        account: str,
        runtime_id: str,
    ) -> None:
        import re

        _app, gateway_stack, _ac = _build_stacks(
            region=region,
            account=account,
            runtime_id=runtime_id,
        )

        template = assertions.Template.from_stack(gateway_stack)
        tpl = template.to_json()

        targets = {
            lid: res
            for lid, res in tpl["Resources"].items()
            if res["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
        }
        assert len(targets) == 1
        _lid, target = next(iter(targets.items()))

        endpoint_value = (
            target.get("Properties", {})
            .get("TargetConfiguration", {})
            .get("Mcp", {})
            .get("McpServer", {})
            .get("Endpoint")
        )
        assert endpoint_value is not None

        resolved = _resolve_endpoint(
            endpoint_value, region=region, account=account, runtime_id=runtime_id,
        )
        assert re.fullmatch(_ENDPOINT_REGEX, resolved), (
            f"Resolved endpoint did not match regex.\n"
            f"  regex: {_ENDPOINT_REGEX}\n"
            f"  resolved: {resolved!r}\n"
            f"  raw: {endpoint_value!r}"
        )
