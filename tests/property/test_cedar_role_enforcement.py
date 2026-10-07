# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: Cedar role enforcement.

Evaluates the REAL policy statements produced by
``scripts/create-policies.py::build_policies`` with the Cedar engine
(cedarpy), using principals shaped like the AgentCore Gateway entity store
for a Cognito ID token (``AgentCore::OAuthUser`` with JWT claims as tags).

Validates: Requirements 2.2, Correctness Property 2
- admin and developer are allowed all six tools
- readonly is allowed only get_task_status and list_tasks
- callers with no role tag, or an unrecognised role value, are denied every
  tool (access requires a positive role match)
- code / run_coding_task on *-production repos are denied for every role
- tools outside the permits and other gateways are denied (default deny)
- the policy tool list matches the @mcp.tool() functions in the server
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import cedarpy
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "create-policies.py"
SERVER_PATH = ROOT / "container" / "code_mcp_server.py"
GW = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/opencode-gateway-test"
OTHER_GW = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/other-gateway"


def _load_module():
    spec = importlib.util.spec_from_file_location("create_policies", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load_module()
POLICIES = cp.build_policies(GW)
POLICY_SET = "\n".join(p["statement"] for p in POLICIES)
# cedarpy names policies policy0..policyN in source order.
POLICY_IDS = {f"policy{i}": p["name"] for i, p in enumerate(POLICIES)}

ALL_TOOLS = (
    "code",
    "run_coding_task",
    "connect_git_host",
    "get_task_status",
    "list_tasks",
    "cancel_task",
)
READONLY_ALLOWED = ("get_task_status", "list_tasks")
READONLY_DENIED = tuple(t for t in ALL_TOOLS if t not in READONLY_ALLOWED)
READONLY_FORBIDDEN = ("code", "run_coding_task", "cancel_task")
REPO_TOOLS = ("code", "run_coding_task")
FULL_ACCESS_ROLES = ("admin", "developer")
UNKNOWN_ROLES = ("", "Admin", "guest", "readonly ", "developers")


def _id_token_tags(role: str | None, extra: dict | None = None) -> dict:
    """Tags as produced from a Cognito ID token (every claim is a string)."""
    tags = {
        "sub": "user-sub",
        "email": "user@example.com",
        "email_verified": "true",
        "cognito:username": "user-sub",
        "cognito:groups": json.dumps(["developer"]),
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
        "token_use": "id",
    }
    if role is not None:
        tags["custom:role"] = role
    tags.update(extra or {})
    return tags


def _tool_input(tool: str, repo_url: str = "https://github.com/org/app") -> dict:
    """Representative tool arguments for each tool."""
    if tool in REPO_TOOLS:
        return {
            "task_description": "fix bug",
            "repo_url": repo_url,
            "base_branch": "main",
            "_user_id": "user-sub",
        }
    if tool == "connect_git_host":
        return {"git_host": "github.com", "_user_id": "user-sub"}
    if tool in ("get_task_status", "cancel_task"):
        return {"job_id": "job-1", "_user_id": "user-sub"}
    return {"_user_id": "user-sub"}


def evaluate(tags: dict, tool: str, tool_input: dict | None, resource: str = GW):
    """Evaluate the real policy set; returns the cedarpy result.

    Fails on any evaluation error because Cedar skips erroring policies,
    which would hide a broken forbid.
    """
    entities = [
        {
            "uid": {"type": "AgentCore::OAuthUser", "id": "user-sub"},
            "attrs": {"id": "user-sub"},
            "parents": [],
            "tags": tags,
        },
        {"uid": {"type": "AgentCore::Gateway", "id": resource}, "attrs": {}, "parents": []},
    ]
    request = {
        "principal": 'AgentCore::OAuthUser::"user-sub"',
        "action": f'AgentCore::Action::"opencode___{tool}"',
        "resource": f'AgentCore::Gateway::"{resource}"',
        "context": {"input": tool_input} if tool_input is not None else {},
    }
    result = cedarpy.is_authorized(request, POLICY_SET, entities)
    assert result.diagnostics.errors == [], result.diagnostics.errors
    return result


def _determining(result) -> set[str]:
    return {POLICY_IDS[r] for r in result.diagnostics.reasons}


repo_urls = st.from_regex(r"https://github\.com/[a-z]{1,10}/[a-z]{1,20}", fullmatch=True)
full_access_roles = st.sampled_from(FULL_ACCESS_ROLES)
all_roles = st.sampled_from(["admin", "developer", "readonly", None])
PRODUCTION_URLS = (
    "https://github.com/org/app-production",
    "https://github.com/org/app-production.git",
    "https://github.com/org/app-production/",
)


class TestCedarRoleEnforcement:
    """Property tests evaluating the real Cedar policies."""

    def test_policy_set_parses(self):
        result = evaluate(_id_token_tags("developer"), "list_tasks", {})
        assert result.decision == cedarpy.Decision.Allow

    @given(role=full_access_roles, tool=st.sampled_from(ALL_TOOLS), repo_url=repo_urls)
    @settings(max_examples=60)
    def test_admin_and_developer_allowed_all_tools(self, role, tool, repo_url):
        result = evaluate(_id_token_tags(role), tool, _tool_input(tool, repo_url))
        assert result.decision == cedarpy.Decision.Allow
        assert _determining(result) == {"opencode_permit_full_access"}

    @given(tool=st.sampled_from(READONLY_ALLOWED))
    @settings(max_examples=20)
    def test_readonly_allowed_status_tools(self, tool):
        result = evaluate(_id_token_tags("readonly"), tool, _tool_input(tool))
        assert result.decision == cedarpy.Decision.Allow
        assert _determining(result) == {"opencode_permit_readonly_status"}

    @given(repo_url=repo_urls, tool=st.sampled_from(READONLY_DENIED))
    @settings(max_examples=50)
    def test_readonly_denied_other_tools(self, repo_url, tool):
        result = evaluate(_id_token_tags("readonly"), tool, _tool_input(tool, repo_url))
        assert result.decision == cedarpy.Decision.Deny
        if tool in READONLY_FORBIDDEN:
            # Defence in depth: an explicit forbid also matches.
            assert _determining(result) & {
                "opencode_readonly_deny_code",
                "opencode_readonly_deny_coding",
                "opencode_readonly_deny_cancel",
            }
        else:
            # connect_git_host: no permit for readonly, so default deny.
            assert result.diagnostics.reasons == []

    @given(tool=st.sampled_from(ALL_TOOLS), repo_url=repo_urls)
    @settings(max_examples=30)
    def test_missing_role_denied_all_tools(self, tool, repo_url):
        result = evaluate(_id_token_tags(None), tool, _tool_input(tool, repo_url))
        assert result.decision == cedarpy.Decision.Deny
        assert result.diagnostics.reasons == []

    @given(role=st.sampled_from(UNKNOWN_ROLES), tool=st.sampled_from(ALL_TOOLS))
    @settings(max_examples=60)
    def test_unknown_role_values_denied_all_tools(self, role, tool):
        result = evaluate(_id_token_tags(role), tool, _tool_input(tool))
        assert result.decision == cedarpy.Decision.Deny

    @given(role=st.sampled_from(["admin", "developer", "readonly"]),
           tool=st.sampled_from(ALL_TOOLS))
    @settings(max_examples=30)
    def test_role_in_other_claim_grants_nothing(self, role, tool):
        """Only the ``custom:role`` tag counts; a role value under a bare
        ``role`` key or in another claim (cognito:groups, username) neither
        grants nor denies."""
        tags = _id_token_tags(
            None,
            {"role": role, "cognito:username": role, "cognito:groups": json.dumps([role])},
        )
        result = evaluate(tags, tool, _tool_input(tool))
        assert result.decision == cedarpy.Decision.Deny
        assert result.diagnostics.reasons == []

    @given(tool=st.sampled_from(ALL_TOOLS))
    @settings(max_examples=30)
    def test_readonly_value_in_other_claim_does_not_deny(self, tool):
        tags = _id_token_tags(
            "developer", {"username": "readonly", "cognito:username": "readonly"}
        )
        result = evaluate(tags, tool, _tool_input(tool))
        assert result.decision == cedarpy.Decision.Allow

    @given(role=all_roles, tool=st.sampled_from(REPO_TOOLS), repo_url=st.sampled_from(PRODUCTION_URLS))
    @settings(max_examples=40)
    def test_production_repo_denied_for_all_roles(self, role, tool, repo_url):
        result = evaluate(_id_token_tags(role), tool, _tool_input(tool, repo_url))
        assert result.decision == cedarpy.Decision.Deny

    @given(role=full_access_roles, tool=st.sampled_from(REPO_TOOLS),
           repo_url=st.sampled_from(PRODUCTION_URLS))
    @settings(max_examples=30)
    def test_production_forbid_wins_over_full_access_permit(self, role, tool, repo_url):
        result = evaluate(_id_token_tags(role), tool, _tool_input(tool, repo_url))
        assert result.decision == cedarpy.Decision.Deny
        assert _determining(result) == {"opencode_deny_production_repos"}

    @given(
        role=full_access_roles,
        tool=st.sampled_from(REPO_TOOLS),
        repo_url=st.sampled_from((
            "https://github.com/org/app-staging",
            "https://github.com/org/app-production-tools",
            "https://github.com/org/production-app",
        )),
    )
    @settings(max_examples=30)
    def test_non_production_repo_allowed(self, role, tool, repo_url):
        result = evaluate(_id_token_tags(role), tool, _tool_input(tool, repo_url))
        assert result.decision == cedarpy.Decision.Allow

    @given(role=all_roles, tool=st.sampled_from(ALL_TOOLS))
    @settings(max_examples=30)
    def test_other_gateway_not_permitted(self, role, tool):
        result = evaluate(
            _id_token_tags(role), tool, _tool_input(tool), resource=OTHER_GW
        )
        assert result.decision == cedarpy.Decision.Deny
        assert result.diagnostics.reasons == []

    def test_unlisted_tool_default_denied(self):
        result = evaluate(_id_token_tags("admin"), "new_tool", {"_user_id": "user-sub"})
        assert result.decision == cedarpy.Decision.Deny
        assert result.diagnostics.reasons == []

    @given(role=st.sampled_from(["admin", "developer", "readonly", None, *UNKNOWN_ROLES]),
           tool=st.sampled_from(ALL_TOOLS))
    @settings(max_examples=40)
    def test_missing_input_evaluates_without_errors(self, role, tool):
        """Guards keep every policy error-free when context.input is absent."""
        evaluate(_id_token_tags(role), tool, None)


class TestPolicyToolCoverage:
    """Drift guards between the policy set and the server's tools."""

    def test_policies_reference_only_real_tools(self):
        """Every action in the policy set is one of the six server tools."""
        referenced = set(re.findall(r'AgentCore::Action::"opencode___(\w+)"', POLICY_SET))
        assert referenced == set(ALL_TOOLS)

    def test_policy_tool_list_matches_server_tools(self):
        """The @mcp.tool() functions in the server are exactly the tools the
        policy script knows about, so a new tool cannot ship without a
        policy decision."""
        tree = ast.parse(SERVER_PATH.read_text())
        server_tools = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "tool"
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "mcp"
                for d in node.decorator_list
            )
        }
        assert server_tools == set(cp.TOOL_NAMES)
        assert set(cp.TOOL_NAMES) == set(ALL_TOOLS)
