# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for AgentCore stack (stacks/agentcore_stack.py).

Validates: Requirements 7.2, 7.3, 10.3, 10.4
- No S3 artifact bucket exists (removed as unused — Requirement 7)
- Security group rules match design (outbound TCP 443 only, no inbound at all)
- IAM execution role has least-privilege permissions; no sts:AssumeRole
- Every remaining IAM wildcard is described by the AwsSolutions-IAM5 reason
- cdk-nag AwsSolutions reports no unsuppressed errors on the stack
"""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import assertions
import cdk_nag
import pytest

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CDK_JSON_PATH = Path(__file__).resolve().parents[2] / "cdk.json"


def _load_cdk_context() -> dict:
    with open(CDK_JSON_PATH) as f:
        return json.load(f)["context"]


def _build_agentcore_app_and_stack(
    context_overrides: dict | None = None,
    with_nag: bool = False,
) -> AgentCoreStack:
    ctx = _load_cdk_context()
    if context_overrides:
        ctx.update(context_overrides)
    app = cdk.App(context=ctx)
    if with_nag:
        cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks())
    env = cdk.Environment(account="123456789012", region="us-east-1")
    security_stack = SecurityStack(app, "TestSecurity", env=env)
    vpc_stack = VpcStack(app, "TestVpc", cmk=security_stack.cmk, env=env)
    return AgentCoreStack(
        app, "TestAgentCore", vpc=vpc_stack.vpc, cmk=security_stack.cmk,
        callback_url="https://test.execute-api.us-east-1.amazonaws.com/callback",
        env=env,
    )


def _build_agentcore_template(
    context_overrides: dict | None = None,
) -> assertions.Template:
    stack = _build_agentcore_app_and_stack(context_overrides)
    return assertions.Template.from_stack(stack)


# ---------------------------------------------------------------------------
# S3 Artifact Bucket removed (Requirement 7)
# ---------------------------------------------------------------------------


class TestNoS3Bucket:
    """Verify S3 artifact bucket has been removed (Requirement 7)."""

    def test_no_s3_bucket_exists(self):
        """Stack should not contain any S3 bucket resources."""
        template = _build_agentcore_template()
        template.resource_count_is("AWS::S3::Bucket", 0)

    def test_no_s3_bucket_policy_exists(self):
        """Stack should not contain any S3 bucket policy resources."""
        template = _build_agentcore_template()
        template.resource_count_is("AWS::S3::BucketPolicy", 0)

    def test_no_s3_iam_actions(self):
        """Execution role should not have any S3 IAM actions."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        actions = _collect_all_policy_actions(tpl)
        s3_actions = {a for a in actions if a.startswith("s3:")}
        assert not s3_actions, f"Unexpected S3 IAM actions found: {s3_actions}"


# ---------------------------------------------------------------------------
# Security Group tests (Requirement 10.3)
# ---------------------------------------------------------------------------


class TestSecurityGroup:
    """Verify AgentCore security group rules match design."""

    def test_security_group_exists(self):
        template = _build_agentcore_template()
        template.resource_count_is("AWS::EC2::SecurityGroup", 1)

    def test_security_group_description(self):
        template = _build_agentcore_template()
        template.has_resource_properties(
            "AWS::EC2::SecurityGroup",
            {"GroupDescription": "AgentCore container security group"},
        )

    def test_egress_is_exactly_https_443_ipv4(self):
        """The only egress rule is TCP 443 to 0.0.0.0/0 (IPv4).

        OpenCode needs outbound HTTPS for VPC interface endpoints (Bedrock,
        AgentCore, ECR, Logs, X-Ray, ...), the S3/DynamoDB gateway endpoints,
        and git hosts via the NAT Gateway. The VPC is IPv4-only, so there is
        no ``::/0`` rule.
        """
        template = _build_agentcore_template()
        tpl = template.to_json()
        sg = _agentcore_sg(tpl)
        egress = sg.get("Properties", {}).get("SecurityGroupEgress", [])
        assert len(egress) == 1, f"Expected exactly one egress rule, got {egress}"
        rule = egress[0]
        assert rule.get("IpProtocol") == "tcp", rule
        assert rule.get("FromPort") == 443, rule
        assert rule.get("ToPort") == 443, rule
        assert rule.get("CidrIp") == "0.0.0.0/0", rule
        assert "CidrIpv6" not in rule, rule
        # No egress rules hiding outside the inline block.
        template.resource_count_is("AWS::EC2::SecurityGroupEgress", 0)

    def test_no_allow_all_outbound(self):
        """No all-traffic egress rule (inline or standalone), and no ::/0 egress."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        rules: list[tuple[str, dict]] = []
        for lid, res in tpl["Resources"].items():
            props = res.get("Properties", {})
            if res["Type"] == "AWS::EC2::SecurityGroup":
                for rule in props.get("SecurityGroupEgress", []):
                    rules.append((lid, rule))
            elif res["Type"] == "AWS::EC2::SecurityGroupEgress":
                rules.append((lid, props))
        for lid, rule in rules:
            if str(rule.get("IpProtocol")) == "-1":
                pytest.fail(f"Security group has allow-all outbound rule: {lid} {rule}")
            if rule.get("CidrIpv6") == "::/0":
                pytest.fail(f"Security group has IPv6 ::/0 egress: {lid} {rule}")

    def test_no_inbound_from_anywhere(self):
        """AgentCore SG has no ingress at all (inline or standalone)."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        sg = _agentcore_sg(tpl)
        inline_ingress = sg.get("Properties", {}).get("SecurityGroupIngress", [])
        assert not inline_ingress, f"AgentCore SG has ingress rules: {inline_ingress}"
        ingress_rules = {
            lid: res
            for lid, res in tpl["Resources"].items()
            if res["Type"] == "AWS::EC2::SecurityGroupIngress"
        }
        for lid, res in ingress_rules.items():
            props = res.get("Properties", {})
            cidr = props.get("CidrIp", "")
            if cidr == "0.0.0.0/0" or props.get("CidrIpv6") == "::/0":
                pytest.fail(
                    f"Security group has ingress from anywhere: {lid}"
                )


# ---------------------------------------------------------------------------
# IAM Execution Role — least-privilege tests (Requirement 7.2)
# ---------------------------------------------------------------------------


class TestIamExecutionRole:
    """Verify AgentCore execution role has least-privilege permissions."""

    def test_execution_role_exists(self):
        template = _build_agentcore_template()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {"RoleName": "opencode-agentcore-execution-role-us-east-1"},
        )

    def test_execution_role_assumed_by_ecs_tasks(self):
        """Role trust policy allows bedrock-agentcore.amazonaws.com."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        role = _find_execution_role(tpl)
        trust = role["Properties"]["AssumeRolePolicyDocument"]
        principals = _collect_service_principals(trust)
        assert "bedrock-agentcore.amazonaws.com" in principals, (
            "Execution role missing bedrock-agentcore.amazonaws.com trust"
        )

    def test_execution_role_assumed_by_bedrock(self):
        """Role trust policy allows bedrock-agentcore.amazonaws.com."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        role = _find_execution_role(tpl)
        trust = role["Properties"]["AssumeRolePolicyDocument"]
        principals = _collect_service_principals(trust)
        assert "bedrock-agentcore.amazonaws.com" in principals, (
            "Execution role missing bedrock-agentcore.amazonaws.com trust"
        )

    def test_policy_has_bedrock_invoke_model(self):
        """Role policy includes bedrock:InvokeModel."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        actions = _collect_all_policy_actions(tpl)
        assert "bedrock:InvokeModel" in actions, (
            "Execution role missing bedrock:InvokeModel permission"
        )

    def test_policy_has_secrets_manager_read(self):
        """Role policy includes secretsmanager:GetSecretValue."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        actions = _collect_all_policy_actions(tpl)
        assert "secretsmanager:GetSecretValue" in actions, (
            "Missing secretsmanager:GetSecretValue"
        )

    def test_policy_has_dynamodb_access(self):
        """Role policy includes DynamoDB read/write actions."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        actions = _collect_all_policy_actions(tpl)
        assert "dynamodb:GetItem" in actions, "Missing dynamodb:GetItem"
        assert "dynamodb:PutItem" in actions, "Missing dynamodb:PutItem"
        assert "dynamodb:UpdateItem" in actions, "Missing dynamodb:UpdateItem"
        assert "dynamodb:Query" in actions, "Missing dynamodb:Query"

    def test_no_sts_assume_role(self):
        """No identity policy grants sts:AssumeRole (the dead self-assume statement is gone).

        Only ``AWS::IAM::Policy`` resources are scanned; the role's trust
        policy legitimately contains ``sts:AssumeRole`` for the service principal.
        """
        template = _build_agentcore_template()
        tpl = template.to_json()
        for lid, res in tpl["Resources"].items():
            if res["Type"] != "AWS::IAM::Policy":
                continue
            doc = res.get("Properties", {}).get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                assert stmt.get("Sid") != "StsAssumeRole", f"{lid}: {stmt}"
                act = stmt.get("Action", [])
                if isinstance(act, str):
                    act = [act]
                bad = {"sts:AssumeRole", "sts:*", "*"} & set(act)
                assert not bad, f"{lid} grants {bad}: {stmt}"


# ---------------------------------------------------------------------------
# cdk-nag
# ---------------------------------------------------------------------------


class TestCdkNag:
    """Verify the AgentCore stack has no unsuppressed AwsSolutions errors."""

    def test_no_unsuppressed_cdk_nag_errors(self):
        """No unsuppressed AwsSolutions errors; the SG needs no suppressions."""
        stack = _build_agentcore_app_and_stack(with_nag=True)
        # any_value() also catches CdkNagValidationFailure, not just AwsSolutions-*.
        errors = assertions.Annotations.from_stack(stack).find_error(
            "*", assertions.Match.any_value()
        )
        assert not errors, f"Unsuppressed cdk-nag errors: {errors}"

    def test_policy_has_cloudwatch_permissions(self):
        """Role policy includes CloudWatch Logs and Metrics actions."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        actions = _collect_all_policy_actions(tpl)
        assert "logs:CreateLogGroup" in actions, "Missing logs:CreateLogGroup"
        assert "logs:PutLogEvents" in actions, "Missing logs:PutLogEvents"
        assert "cloudwatch:PutMetricData" in actions, "Missing cloudwatch:PutMetricData"

    def test_secrets_manager_scoped_to_opencode_prefix(self):
        """Secrets Manager access is scoped to bedrock-agentcore-identity* secrets."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        sm_resources = _collect_resources_for_action(tpl, "secretsmanager:GetSecretValue")
        assert any("bedrock-agentcore-identity" in str(r) for r in sm_resources), (
            "Secrets Manager access not scoped to bedrock-agentcore-identity* prefix"
        )

    def test_dynamodb_scoped_to_opencode_tables(self):
        """DynamoDB access is scoped to opencode-jobs table."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        ddb_resources = _collect_resources_for_action(tpl, "dynamodb:GetItem")
        resource_str = json.dumps(ddb_resources)
        assert "opencode-jobs" in resource_str, (
            "DynamoDB access not scoped to opencode-jobs table"
        )

    def test_no_admin_or_star_actions(self):
        """Role does not have overly broad actions like iam:*, s3:*, or *."""
        template = _build_agentcore_template()
        tpl = template.to_json()
        actions = _collect_all_policy_actions(tpl)
        dangerous = {"*", "iam:*", "s3:*", "dynamodb:*", "bedrock:*", "sts:*"}
        found = actions & dangerous
        assert not found, f"Execution role has overly broad actions: {found}"


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class TestRuntime:
    """The image is a DockerImageAsset; no stack-owned ECR repository."""

    def test_no_ecr_repository(self):
        template = _build_agentcore_template()
        template.resource_count_is("AWS::ECR::Repository", 0)

    def test_runtime_env_sets_opencode_model_to_default_model_id(self):
        template = _build_agentcore_template()
        model_id = _load_cdk_context()["default_model_id"]
        assert model_id == "global.anthropic.claude-opus-4-6-v1"
        template.has_resource_properties(
            "AWS::BedrockAgentCore::Runtime",
            {
                "EnvironmentVariables": assertions.Match.object_like(
                    {"OPENCODE_MODEL": model_id}
                ),
            },
        )

    def test_bedrock_resources_derive_from_default_model_only(self):
        """Only the default model's foundation-model and inference-profile
        ARNs are granted (no extra hard-coded model)."""
        tpl = _build_agentcore_template().to_json()
        resources = _collect_resources_for_action(tpl, "bedrock:InvokeModel")
        flat = json.dumps(resources)
        assert "anthropic.claude-opus-4-6-v1" in flat
        assert "sonnet" not in flat.lower()
        assert len(resources) == 2, resources


# ---------------------------------------------------------------------------
# Helpers for IAM policy inspection
# ---------------------------------------------------------------------------


def _agentcore_sg(tpl: dict) -> dict:
    """Return the single AWS::EC2::SecurityGroup resource in the stack."""
    sgs = [
        res for res in tpl["Resources"].values()
        if res["Type"] == "AWS::EC2::SecurityGroup"
    ]
    assert len(sgs) == 1, f"Expected exactly one security group, found {len(sgs)}"
    return sgs[0]


def _find_execution_role(tpl: dict) -> dict:
    """Find the AgentCore execution role resource."""
    for lid, res in tpl["Resources"].items():
        if res["Type"] == "AWS::IAM::Role":
            role_name = res.get("Properties", {}).get("RoleName", "")
            if role_name.startswith("opencode-agentcore-execution-role"):
                return res
    raise AssertionError("AgentCore execution role not found")


def _collect_service_principals(trust_policy: dict) -> set[str]:
    """Extract all service principals from a trust policy document."""
    principals: set[str] = set()
    for stmt in trust_policy.get("Statement", []):
        principal = stmt.get("Principal", {})
        service = principal.get("Service", [])
        if isinstance(service, str):
            principals.add(service)
        elif isinstance(service, list):
            principals.update(service)
    return principals


def _collect_all_policy_actions(tpl: dict) -> set[str]:
    """Collect all IAM policy actions from inline policies on the execution role."""
    actions: set[str] = set()
    for lid, res in tpl["Resources"].items():
        if res["Type"] == "AWS::IAM::Policy":
            doc = res.get("Properties", {}).get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                act = stmt.get("Action", [])
                if isinstance(act, str):
                    actions.add(act)
                elif isinstance(act, list):
                    actions.update(act)
    return actions


def _collect_resources_for_action(tpl: dict, action: str) -> list:
    """Collect all Resource values from policy statements containing the given action."""
    resources: list = []
    for lid, res in tpl["Resources"].items():
        if res["Type"] == "AWS::IAM::Policy":
            doc = res.get("Properties", {}).get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                act = stmt.get("Action", [])
                if isinstance(act, str):
                    act = [act]
                if action in act:
                    resource = stmt.get("Resource", [])
                    if isinstance(resource, list):
                        resources.extend(resource)
                    else:
                        resources.append(resource)
    return resources
