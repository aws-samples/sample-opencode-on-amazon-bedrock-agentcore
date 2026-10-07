# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for CDK app instantiation (app.py).

Validates:
- All stacks are created and dependencies are wired
- cdk.json context values are read correctly
- cdk-nag AwsSolutions aspect is applied

Six stacks: VPC, Security, JobStore, CallbackApi, AgentCore, Gateway. The
Cedar policy engine lives in the Gateway stack and the workload identity in
the CallbackApi stack.
"""

import json
from pathlib import Path

import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.job_store_stack import JobStoreStack
from stacks.callback_api_stack import CallbackApiStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.gateway_stack import GatewayStack
from stacks import apply_standard_tags

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CDK_JSON_PATH = Path(__file__).resolve().parents[2] / "cdk.json"


def _load_cdk_context() -> dict:
    """Load the cdk.json context block."""
    with open(CDK_JSON_PATH) as f:
        return json.load(f)["context"]


def _build_app(context_overrides: dict | None = None) -> cdk.App:
    """Replicate the app.py wiring logic and return the CDK App."""
    ctx = _load_cdk_context()
    if context_overrides:
        ctx.update(context_overrides)

    app = cdk.App(context=ctx)
    env = cdk.Environment(account="123456789012", region="us-east-1")

    security_stack = SecurityStack(app, "OpenCodeSecurity", env=env)

    vpc_stack = VpcStack(app, "OpenCodeVpc", cmk=security_stack.cmk, env=env)
    vpc_stack.add_dependency(security_stack)

    job_store_stack = JobStoreStack(
        app, "OpenCodeJobStore", cmk=security_stack.cmk, env=env,
    )
    job_store_stack.add_dependency(security_stack)

    callback_api_stack = CallbackApiStack(
        app, "OpenCodeCallbackApi", cmk=security_stack.cmk, env=env,
    )
    callback_api_stack.add_dependency(security_stack)

    agentcore_stack = AgentCoreStack(
        app, "OpenCodeAgentCore",
        vpc=vpc_stack.vpc, cmk=security_stack.cmk,
        callback_url=callback_api_stack.callback_url_value,
        env=env,
    )
    agentcore_stack.add_dependency(vpc_stack)
    agentcore_stack.add_dependency(security_stack)
    agentcore_stack.add_dependency(callback_api_stack)

    gateway_stack = GatewayStack(
        app, "OpenCodeGateway",
        cognito_user_pool=security_stack.user_pool,
        cognito_client_id=security_stack.user_pool_client.user_pool_client_id,
        opencode_runtime=agentcore_stack.runtime,
        cmk=security_stack.cmk,
        env=env,
    )
    gateway_stack.add_dependency(security_stack)
    gateway_stack.add_dependency(agentcore_stack)

    apply_standard_tags(app)
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

    return app


def _stack_names(app: cdk.App) -> list[str]:
    return [s.stack_name for s in app.node.children if isinstance(s, cdk.Stack)]


def _get_stack(app: cdk.App, name: str) -> cdk.Stack:
    for child in app.node.children:
        if isinstance(child, cdk.Stack) and child.stack_name == name:
            return child
    raise KeyError(f"Stack {name} not found")


def _dep_names(stack: cdk.Stack) -> set[str]:
    return {d.stack_name for d in stack.dependencies}


# ---------------------------------------------------------------------------
# Stack presence tests (6 stacks)
# ---------------------------------------------------------------------------

CORE_STACK_IDS = [
    "OpenCodeVpc",
    "OpenCodeSecurity",
    "OpenCodeJobStore",
    "OpenCodeCallbackApi",
    "OpenCodeAgentCore",
    "OpenCodeGateway",
]


class TestStackCreation:
    """Verify all stacks are instantiated."""

    def test_all_core_stacks_present(self):
        app = _build_app()
        names = _stack_names(app)
        for sid in CORE_STACK_IDS:
            assert sid in names, f"Missing stack: {sid}"

    def test_total_stack_count(self):
        app = _build_app()
        assert sorted(_stack_names(app)) == sorted(CORE_STACK_IDS)


# ---------------------------------------------------------------------------
# Dependency wiring tests
# ---------------------------------------------------------------------------

class TestStackDependencies:
    """Verify dependency ordering between stacks."""

    def test_job_store_depends_on_security(self):
        app = _build_app()
        assert "OpenCodeSecurity" in _dep_names(_get_stack(app, "OpenCodeJobStore"))

    def test_agentcore_depends_on_vpc_and_security(self):
        app = _build_app()
        deps = _dep_names(_get_stack(app, "OpenCodeAgentCore"))
        assert "OpenCodeVpc" in deps
        assert "OpenCodeSecurity" in deps

    def test_agentcore_depends_on_callback_api(self):
        app = _build_app()
        assert "OpenCodeCallbackApi" in _dep_names(_get_stack(app, "OpenCodeAgentCore"))

    def test_gateway_depends_on_security_and_agentcore(self):
        app = _build_app()
        deps = _dep_names(_get_stack(app, "OpenCodeGateway"))
        assert "OpenCodeSecurity" in deps
        assert "OpenCodeAgentCore" in deps

    def test_gateway_has_policy_engine_in_stack(self):
        """The Cedar policy engine is a resource of the Gateway stack."""
        from aws_cdk import assertions

        app = _build_app()
        template = assertions.Template.from_stack(_get_stack(app, "OpenCodeGateway"))
        template.resource_count_is("AWS::BedrockAgentCore::PolicyEngine", 1)


# ---------------------------------------------------------------------------
# cdk.json context value tests
# ---------------------------------------------------------------------------

class TestCdkContext:
    """Verify cdk.json context values are read correctly."""

    def test_default_model_id(self):
        ctx = _load_cdk_context()
        assert ctx["default_model_id"] == "global.anthropic.claude-opus-4-6-v1"

    def test_task_timeout_defaults(self):
        ctx = _load_cdk_context()
        assert ctx["task_timeout_minutes_default"] == 10
        assert ctx["task_timeout_minutes_max"] == 30

    def test_retention_days(self):
        ctx = _load_cdk_context()
        assert ctx["cloudwatch_log_retention_days"] == 90

    def test_daily_cost_budget(self):
        ctx = _load_cdk_context()
        assert ctx["daily_cost_budget_usd"] == 50

    def test_cloudtrail_disabled_by_default(self):
        ctx = _load_cdk_context()
        assert ctx["enable_cloudtrail"] is False

    def test_account_and_region_empty_by_default(self):
        ctx = _load_cdk_context()
        assert ctx["account"] == ""
        assert ctx["region"] == ""

    def test_context_values_accessible_from_app(self):
        """Verify the app can read context values via try_get_context."""
        app = _build_app()
        assert app.node.try_get_context("default_model_id") == \
            "global.anthropic.claude-opus-4-6-v1"
        assert app.node.try_get_context("task_timeout_minutes_default") == 10


# ---------------------------------------------------------------------------
# cdk-nag aspect test
# ---------------------------------------------------------------------------

class TestSynth:
    """Verify CDK app synthesizes all stacks successfully."""

    def test_synth_succeeds(self):
        app = _build_app()
        assembly = app.synth()
        stack_names = [s.stack_name for s in assembly.stacks]
        for sid in CORE_STACK_IDS:
            assert sid in stack_names, f"Stack {sid} missing from synth output"


class TestCdkNag:
    """Verify cdk-nag AwsSolutions aspect is applied to the app."""

    def test_aws_solutions_aspect_applied(self):
        app = _build_app()
        all_aspects = cdk.Aspects.of(app).all
        nag_found = any(
            isinstance(a, cdk_nag.AwsSolutionsChecks) for a in all_aspects
        )
        assert nag_found, "cdk-nag AwsSolutionsChecks aspect not applied"
