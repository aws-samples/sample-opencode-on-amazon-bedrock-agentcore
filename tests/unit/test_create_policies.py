# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for scripts/create-policies.py.

Validates:
- build_policies() returns the expected six policies with valid names
- gateway ARN validation before interpolation into Cedar text
- _ensure_policy() create / skip / update-in-place behavior, gated on the
  status of a fresh GetPolicy response (not the ListPolicies summary)
- main() keeps the --region CLI, reads both stack outputs from
  OpenCodeGateway, and applies forbids before the permits
- --delete removes only the managed policies, waits for deletion, and is
  idempotent
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "create-policies.py"
GW = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/opencode-gateway-test"


def _load_module():
    spec = importlib.util.spec_from_file_location("create_policies", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cp(monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return mod


def _client(policies: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{"policies": policies or []}]
    client.create_policy.return_value = {"policyId": "pol-new"}
    return client


def _by_name(cp, name: str) -> str:
    return next(p["statement"] for p in cp.build_policies(GW) if p["name"] == name)


def _active(statement: str, policy_id: str = "pol-1") -> dict:
    return {
        "policyId": policy_id,
        "status": "ACTIVE",
        "definition": {"cedar": {"statement": statement}},
    }


# ---------------------------------------------------------------------------
# build_policies
# ---------------------------------------------------------------------------

class TestBuildPolicies:

    def test_six_policies_with_expected_keys(self, cp):
        policies = cp.build_policies(GW)
        assert len(policies) == 6
        for p in policies:
            assert set(p) == {"name", "description", "statement"}

    def test_policy_names_and_order(self, cp):
        assert [p["name"] for p in cp.build_policies(GW)] == [
            "opencode_readonly_deny_coding",
            "opencode_readonly_deny_cancel",
            "opencode_readonly_deny_code",
            "opencode_deny_production_repos",
            "opencode_permit_full_access",
            "opencode_permit_readonly_status",
        ]

    def test_names_unique_and_valid(self, cp):
        names = [p["name"] for p in cp.build_policies(GW)]
        assert len(set(names)) == len(names)
        for name in names:
            assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name), name
            assert len(name) <= 48

    def test_existing_names_preserved(self, cp):
        names = [p["name"] for p in cp.build_policies(GW)]
        assert "opencode_readonly_deny_coding" in names
        assert "opencode_readonly_deny_cancel" in names
        assert "opencode_readonly_deny_code" in names

    def test_every_statement_scoped_to_gateway(self, cp):
        for p in cp.build_policies(GW):
            assert f'resource == AgentCore::Gateway::"{GW}"' in p["statement"]

    def test_permits_are_last(self, cp):
        policies = cp.build_policies(GW)
        effects = [p["statement"].split("(", 1)[0] for p in policies]
        assert effects == ["forbid"] * 4 + ["permit"] * 2

    def test_full_access_permit_lists_all_six_actions(self, cp):
        permit = _by_name(cp, "opencode_permit_full_access")
        for tool in cp.TOOL_NAMES:
            assert f'AgentCore::Action::"opencode___{tool}"' in permit
        assert len(cp.TOOL_NAMES) == 6

    def test_readonly_permit_lists_only_status_tools(self, cp):
        permit = _by_name(cp, "opencode_permit_readonly_status")
        actions = set(re.findall(r'AgentCore::Action::"opencode___(\w+)"', permit))
        assert actions == {"get_task_status", "list_tasks"}

    def test_permits_are_role_gated_on_custom_role_only(self, cp):
        """Every permit has a hasTag-guarded when-clause on ``custom:role``
        and does not match a bare ``role`` tag."""
        for name, roles in (
            ("opencode_permit_full_access", ("admin", "developer")),
            ("opencode_permit_readonly_status", ("readonly",)),
        ):
            permit = _by_name(cp, name)
            assert ") when {" in permit
            assert 'principal.hasTag("custom:role") &&' in permit
            for role in roles:
                assert f'principal.getTag("custom:role") == "{role}"' in permit
            assert 'getTag("role")' not in permit
            assert 'hasTag("role")' not in permit

    def test_readonly_forbid_statement_text(self, cp):
        stmt = _by_name(cp, "opencode_readonly_deny_code")
        assert stmt == (
            "forbid(\n"
            "  principal,\n"
            '  action == AgentCore::Action::"opencode___code",\n'
            f'  resource == AgentCore::Gateway::"{GW}"\n'
            ") when {\n"
            '  principal.hasTag("custom:role") && principal.getTag("custom:role") == "readonly"\n'
            "};"
        )

    def test_no_submit_input(self, cp):
        for p in cp.build_policies(GW):
            assert "submit_input" not in p["statement"]

    @pytest.mark.parametrize(
        "bad_arn",
        [
            'arn:aws:bedrock-agentcore:us-east-1:1:gateway/x"',
            "arn:aws:bedrock-agentcore:us-east-1:1:gateway/x\nfoo",
            "arn:aws:bedrock-agentcore:us-east-1:1:gateway/x\\",
            "not-an-arn",
            "",
        ],
    )
    def test_invalid_arn_rejected(self, cp, bad_arn):
        with pytest.raises(ValueError):
            cp.build_policies(bad_arn)


# ---------------------------------------------------------------------------
# _ensure_policy
# ---------------------------------------------------------------------------

class TestEnsurePolicy:

    def test_absent_creates(self, cp):
        client = _client()
        client.get_policy.return_value = {"status": "ACTIVE"}
        cp._ensure_policy(client, "eng", name="p1", statement="permit(principal, action, resource);",
                          description="d")
        client.create_policy.assert_called_once()
        kwargs = client.create_policy.call_args.kwargs
        assert kwargs["validationMode"] == "IGNORE_ALL_FINDINGS"
        assert kwargs["name"] == "p1"
        assert kwargs["definition"] == {"cedar": {"statement": "permit(principal, action, resource);"}}
        client.update_policy.assert_not_called()

    def test_same_statement_different_whitespace_skips(self, cp):
        stmt = "forbid(\n  principal,\n  action,\n  resource\n);"
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "ACTIVE"}])
        client.get_policy.return_value = _active("forbid( principal, action,   resource );")
        cp._ensure_policy(client, "eng", name="p1", statement=stmt, description="d")
        client.create_policy.assert_not_called()
        client.update_policy.assert_not_called()

    def test_changed_statement_updates_in_place(self, cp):
        new_stmt = 'forbid(principal, action, resource) when { principal.hasTag("custom:role") };'
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "ACTIVE"}])
        client.get_policy.side_effect = [
            _active("forbid(principal, action, resource);"),
            {"status": "UPDATING"},
            {"status": "ACTIVE"},
        ]
        cp._ensure_policy(client, "eng", name="p1", statement=new_stmt, description="d")
        client.create_policy.assert_not_called()
        client.update_policy.assert_called_once()
        kwargs = client.update_policy.call_args.kwargs
        assert kwargs["policyId"] == "pol-1"
        assert kwargs["definition"] == {"cedar": {"statement": new_stmt}}
        assert kwargs["validationMode"] == "IGNORE_ALL_FINDINGS"
        assert client.get_policy.call_count == 3

    def test_failed_policies_ignored_when_matching_name(self, cp):
        client = _client([{"name": "p1", "policyId": "old", "status": "CREATE_FAILED"}])
        client.get_policy.return_value = {"status": "ACTIVE"}
        cp._ensure_policy(client, "eng", name="p1", statement="s", description="d")
        client.create_policy.assert_called_once()

    def test_wait_raises_on_failed(self, cp):
        client = _client()
        client.get_policy.return_value = {"status": "CREATE_FAILED", "statusReasons": ["bad"]}
        with pytest.raises(RuntimeError):
            cp._ensure_policy(client, "eng", name="p1", statement="s", description="d")

    def test_wait_times_out(self, cp):
        client = _client()
        client.get_policy.return_value = {"status": "CREATING"}
        with pytest.raises(TimeoutError):
            cp._ensure_policy(client, "eng", name="p1", statement="s", description="d")

    def test_returns_policy_id(self, cp):
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "ACTIVE"}])
        client.get_policy.return_value = _active("s")
        assert cp._ensure_policy(client, "eng", name="p1", statement="s",
                                 description="d") == "pol-1"
        client = _client()
        client.get_policy.return_value = {"status": "ACTIVE"}
        assert cp._ensure_policy(client, "eng", name="p1", statement="s",
                                 description="d") == "pol-new"

    def test_active_in_list_but_updating_in_get_waits(self, cp):
        """The fresh GetPolicy status gates, not the list summary: an
        UPDATING response is waited on before the policy counts as ACTIVE."""
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "ACTIVE"}])
        updating = dict(_active("s"), status="UPDATING")
        client.get_policy.side_effect = [updating, updating, _active("s")]
        assert cp._ensure_policy(client, "eng", name="p1", statement="s",
                                 description="d") == "pol-1"
        assert client.get_policy.call_count == 3
        client.update_policy.assert_not_called()
        client.create_policy.assert_not_called()

    @pytest.mark.parametrize("status", ["UPDATE_FAILED", "CREATE_FAILED"])
    def test_active_in_list_but_failed_in_get_raises(self, cp, status):
        """Matching statement text with a failure status is not 'ACTIVE'."""
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "ACTIVE"}])
        client.get_policy.return_value = dict(
            _active("s"), status=status, statusReasons=["bad"],
        )
        with pytest.raises(RuntimeError, match="FAILED"):
            cp._ensure_policy(client, "eng", name="p1", statement="s", description="d")
        client.update_policy.assert_not_called()

    def test_active_in_list_but_deleting_in_get_raises(self, cp):
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "ACTIVE"}])
        client.get_policy.return_value = dict(_active("s"), status="DELETING")
        with pytest.raises(RuntimeError, match="DELETING"):
            cp._ensure_policy(client, "eng", name="p1", statement="s", description="d")
        client.update_policy.assert_not_called()

    def test_updating_in_list_then_failed_raises(self, cp):
        client = _client([{"name": "p1", "policyId": "pol-1", "status": "UPDATING"}])
        client.get_policy.side_effect = [
            {"status": "UPDATING"},
            {"status": "UPDATE_FAILED", "statusReasons": ["bad"]},
        ]
        with pytest.raises(RuntimeError, match="FAILED"):
            cp._ensure_policy(client, "eng", name="p1", statement="s", description="d")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

class TestMain:

    @staticmethod
    def _wire(cp, monkeypatch, agentcore):
        monkeypatch.setattr(cp.boto3, "client", lambda service, **_: (
            agentcore if service == "bedrock-agentcore-control" else MagicMock()
        ))
        monkeypatch.setattr(cp, "_get_stack_outputs", lambda _cfn, stack: (
            {"PolicyEngineId": "eng-1", "GatewayArn": GW}
            if stack == "OpenCodeGateway" else {}
        ))
        monkeypatch.setattr(sys, "argv", ["create-policies.py", "--region", "us-east-1"])

    def test_main_applies_all_policies_in_order(self, cp, monkeypatch):
        agentcore = _client()
        agentcore.get_policy.return_value = {"status": "ACTIVE"}
        self._wire(cp, monkeypatch, agentcore)

        cp.main()

        created = [c.kwargs["name"] for c in agentcore.create_policy.call_args_list]
        assert created == [p["name"] for p in cp.build_policies(GW)]
        assert created[-2:] == [
            "opencode_permit_full_access", "opencode_permit_readonly_status",
        ]
        agentcore.delete_policy.assert_not_called()

    def test_main_reads_outputs_from_gateway_stack(self, cp, monkeypatch):
        agentcore = _client()
        agentcore.get_policy.return_value = {"status": "ACTIVE"}
        self._wire(cp, monkeypatch, agentcore)
        stacks: list[str] = []
        monkeypatch.setattr(cp, "_get_stack_outputs", lambda _cfn, stack: (
            stacks.append(stack) or {"PolicyEngineId": "eng-1", "GatewayArn": GW}
        ))

        cp.main()

        assert stacks == ["OpenCodeGateway"]
        for c in agentcore.create_policy.call_args_list:
            assert c.kwargs["policyEngineId"] == "eng-1"

    def test_main_requires_region(self, cp, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["create-policies.py"])
        with pytest.raises(SystemExit):
            cp.main()


class _NotFound(Exception):
    pass


def _deleting_client(policies: list[dict]) -> MagicMock:
    """Client whose get_policy raises not-found once a policy was deleted."""
    client = _client(policies)
    client.exceptions.ResourceNotFoundException = _NotFound
    deleted: set[str] = set()

    def _delete(policyEngineId, policyId):
        deleted.add(policyId)

    def _get(policyEngineId, policyId):
        if policyId in deleted:
            raise _NotFound()
        return {"policyId": policyId, "status": "DELETING"}

    client.delete_policy.side_effect = _delete
    client.get_policy.side_effect = _get
    return client


class TestDeletePolicies:

    def test_deletes_only_managed_policies_and_waits(self, cp):
        managed = [p["name"] for p in cp.build_policies(GW)]
        policies = [
            {"name": n, "policyId": f"p-{i}", "status": "ACTIVE"}
            for i, n in enumerate(managed)
        ] + [{"name": "someone_elses_policy", "policyId": "p-other", "status": "ACTIVE"}]
        client = _deleting_client(policies)

        cp.delete_policies(client, "eng", GW)

        deleted = [c.kwargs["policyId"] for c in client.delete_policy.call_args_list]
        assert deleted == [f"p-{i}" for i in range(len(managed))]
        assert "p-other" not in deleted
        # Every deletion was confirmed by a GetPolicy that raised not-found.
        assert client.get_policy.call_count >= len(managed)

    def test_idempotent_when_nothing_to_delete(self, cp):
        client = _deleting_client([])
        cp.delete_policies(client, "eng", GW)
        client.delete_policy.assert_not_called()

    def test_main_delete_flag_skips_create(self, cp, monkeypatch):
        client = _deleting_client([
            {"name": "opencode_permit_full_access", "policyId": "p-1", "status": "ACTIVE"},
        ])
        TestMain._wire(cp, monkeypatch, client)
        monkeypatch.setattr(
            sys, "argv", ["create-policies.py", "--region", "us-east-1", "--delete"],
        )

        cp.main()

        client.create_policy.assert_not_called()
        client.update_policy.assert_not_called()
        assert [c.kwargs["policyId"] for c in client.delete_policy.call_args_list] == ["p-1"]


class TestCleanupFailed:

    def test_deletes_failed_policies(self, cp):
        client = _client([
            {"name": "some_policy", "policyId": "p-failed", "status": "UPDATE_FAILED"},
            {"name": "another", "policyId": "p-cfailed", "status": "CREATE_FAILED"},
            {"name": "other_policy", "policyId": "p-ok", "status": "ACTIVE"},
        ])
        cp._cleanup_failed(client, "eng")
        deleted = {c.kwargs["policyId"] for c in client.delete_policy.call_args_list}
        assert deleted == {"p-failed", "p-cfailed"}
