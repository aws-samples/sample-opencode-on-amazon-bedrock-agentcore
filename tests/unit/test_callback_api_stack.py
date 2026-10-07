# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the CallbackApi stack (stacks/callback_api_stack.py).

Covers the AgentCore workload identity that lives in this stack and the
CMK encryption of every log group it creates.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_kms as kms

from stacks.callback_api_stack import CallbackApiStack

_REGION = "us-east-1"
_ACCOUNT = "123456789012"
_CDK_JSON_PATH = Path(__file__).resolve().parents[2] / "cdk.json"


def _load_cdk_context() -> dict:
    with open(_CDK_JSON_PATH) as f:
        return json.load(f)["context"]


def _build_template() -> assertions.Template:
    ctx = _load_cdk_context()
    app = cdk.App(context=ctx)
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    cmk_stack = cdk.Stack(app, "StubCmkStack", env=env)
    stub_cmk = kms.Key(cmk_stack, "StubCmk")
    stack = CallbackApiStack(app, "OpenCodeCallbackApi", cmk=stub_cmk, env=env)
    return assertions.Template.from_stack(stack)


class TestWorkloadIdentity:
    def test_single_workload_identity_named_opencode_runtime(self) -> None:
        template = _build_template()
        template.resource_count_is("AWS::BedrockAgentCore::WorkloadIdentity", 1)
        template.has_resource_properties(
            "AWS::BedrockAgentCore::WorkloadIdentity",
            {"Name": "opencode_runtime"},
        )

    def test_return_url_is_the_callback_url(self) -> None:
        tpl = _build_template().to_json()
        wi = next(
            r for r in tpl["Resources"].values()
            if r["Type"] == "AWS::BedrockAgentCore::WorkloadIdentity"
        )
        urls = wi["Properties"]["AllowedResourceOauth2ReturnUrls"]
        assert len(urls) == 1
        # The URL is an Fn::Join over the HTTP API invoke URL ending in "callback".
        assert urls[0]["Fn::Join"][1][-1] == "/callback"

    def test_outputs_present(self) -> None:
        tpl = _build_template().to_json()
        outputs = tpl.get("Outputs", {})
        assert "WorkloadIdentityName" in outputs
        assert "WorkloadIdentityArn" in outputs
        assert "OAuthCallbackUrl" in outputs

    def test_no_custom_resource_provider(self) -> None:
        """Credential providers are registered by scripts/setup-oauth-app.sh,
        not by a custom resource."""
        tpl = _build_template().to_json()
        fns = [
            r for r in tpl["Resources"].values()
            if r["Type"] == "AWS::Lambda::Function"
        ]
        # Only the callback handler and its authorizer.
        assert len(fns) == 2, [f["Properties"].get("Handler") for f in fns]
        assert not any(
            r["Type"].startswith("AWS::CloudFormation::CustomResource")
            or r["Type"] == "Custom::AWS"
            for r in tpl["Resources"].values()
        )


class TestLogGroupsEncryptedWithCmk:
    def test_all_log_groups_have_kms_key_id(self) -> None:
        tpl = _build_template().to_json()
        log_groups = {
            lid: res
            for lid, res in tpl["Resources"].items()
            if res.get("Type") == "AWS::Logs::LogGroup"
        }
        assert len(log_groups) >= 3
        for lid, res in log_groups.items():
            assert "KmsKeyId" in res.get("Properties", {}), lid
