#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Post-deploy: create Cedar policies in the PolicyEngine via boto3.

The CfnPolicy CloudFormation resource handler has stabilization issues,
so policies are managed via the API instead.

Action names use the {target}___{tool} format per AgentCore Cedar schema.

Policy set (built by ``build_policies``, applied in this order):
  1. opencode_readonly_deny_coding   - forbid run_coding_task for role "readonly"
  2. opencode_readonly_deny_cancel   - forbid cancel_task for role "readonly"
  3. opencode_readonly_deny_code     - forbid code for role "readonly"
  4. opencode_deny_production_repos  - forbid code / run_coding_task when
                                       repo_url ends in -production
  5. opencode_permit_full_access     - permit roles "admin" and "developer"
                                       on the six opencode tools
  6. opencode_permit_readonly_status - permit role "readonly" on
                                       get_task_status and list_tasks

Cedar is default-deny and forbid overrides permit. Access requires a
positive role match, so once the Gateway is switched from LOG_ONLY to
ENFORCE a caller with no role tag or an unrecognised role value is denied
every tool, and readonly is denied every tool except get_task_status and
list_tasks (connect_git_host has no permit for readonly). The readonly
forbids are kept as defence in depth. The Gateway itself stays in LOG_ONLY
mode (set in stacks/gateway_stack.py); this script does not change the mode.

Re-running is safe: policies are matched by name. Missing policies are created,
policies whose statement changed are updated in place, unchanged ones are
skipped. Policies in a FAILED state are deleted and recreated; no other
policy is deleted.

Usage:
    python scripts/create-policies.py --region us-east-1
    python scripts/create-policies.py --region us-east-1 --delete

``--delete`` removes the six policies listed above (matched by exact name)
and waits until they are gone. Run it before ``cdk destroy``: the policy
engine cannot be deleted while it still contains policies, so the
OpenCodeGateway stack ends in DELETE_FAILED otherwise. Idempotent.

Reads PolicyEngineId and GatewayArn from the OpenCodeGateway CloudFormation
stack outputs.
"""

import argparse
import time

import boto3

# Gateway target name; Cedar action ids are "{TARGET_NAME}___{tool}".
TARGET_NAME = "opencode"
TOOL_NAMES = (
    "code",
    "run_coding_task",
    "connect_git_host",
    "get_task_status",
    "list_tasks",
    "cancel_task",
)
FULL_ACCESS_ROLES = ("admin", "developer")
READONLY_ROLE = "readonly"
# Tools readonly is permitted to call. Every other tool is denied to readonly
# by default-deny; code, run_coding_task and cancel_task are also forbidden
# explicitly below.
READONLY_PERMITTED_TOOLS = ("get_task_status", "list_tasks")
# JWT claims become principal tags keyed by claim name. A Cognito ID token
# carries the custom attribute as "custom:role".
ROLE_TAG_KEY = "custom:role"
PRODUCTION_REPO_PATTERNS = ("*-production", "*-production.git", "*-production/")
VALIDATION_MODE = "IGNORE_ALL_FINDINGS"


def _action(tool: str) -> str:
    """Return the Cedar action entity reference for an opencode tool."""
    return f'AgentCore::Action::"{TARGET_NAME}___{tool}"'


def _role_condition(roles: tuple[str, ...]) -> str:
    """Cedar condition that is true when the caller's role tag is in ``roles``.

    Guarded with hasTag so evaluation never errors when the tag is absent.
    Values are matched exactly (case-sensitive, no trimming).
    """
    checks = " || ".join(
        f'principal.getTag("{ROLE_TAG_KEY}") == "{r}"' for r in roles
    )
    if len(roles) > 1:
        checks = f"({checks})"
    return f'  principal.hasTag("{ROLE_TAG_KEY}") && {checks}'


def _readonly_condition() -> str:
    """Cedar condition that is true when the caller's role tag is readonly."""
    return _role_condition((READONLY_ROLE,))


def _production_condition() -> str:
    """Cedar condition that is true when repo_url names a *-production repo."""
    likes = " ||\n   ".join(
        f'context.input.repo_url like "{pattern}"' for pattern in PRODUCTION_REPO_PATTERNS
    )
    return (
        "  context has input && context.input has repo_url &&\n"
        f"  ({likes})"
    )


def _validate_gateway_arn(gateway_arn: str) -> None:
    """Reject ARNs that are not safe to embed in Cedar source text."""
    if not isinstance(gateway_arn, str) or not gateway_arn.startswith("arn:aws"):
        raise ValueError(f"Invalid gateway ARN: {gateway_arn!r}")
    if any(ch in gateway_arn for ch in ('"', "\\", "\n", "\r")):
        raise ValueError(f"Gateway ARN contains disallowed characters: {gateway_arn!r}")


def _readonly_forbid(tool: str, gateway_arn: str) -> str:
    return (
        "forbid(\n"
        "  principal,\n"
        f"  action == {_action(tool)},\n"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ") when {\n"
        f"{_readonly_condition()}\n"
        "};"
    )


def _role_permit(tools: tuple[str, ...], roles: tuple[str, ...], gateway_arn: str) -> str:
    actions = ",\n".join(f"    {_action(t)}" for t in tools)
    return (
        "permit(\n"
        "  principal is AgentCore::OAuthUser,\n"
        "  action in [\n"
        f"{actions}\n"
        "  ],\n"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ") when {\n"
        f"{_role_condition(roles)}\n"
        "};"
    )


def build_policies(gateway_arn: str) -> list[dict]:
    """Return the Cedar policy definitions for this gateway.

    Pure function (no AWS calls). Each item has ``name``, ``description`` and
    ``statement``. Forbid policies come first and the permits last, so a run
    against an ENFORCE-mode gateway never has a permit active without the
    forbids.
    """
    _validate_gateway_arn(gateway_arn)
    resource = f'AgentCore::Gateway::"{gateway_arn}"'

    production_actions = ",\n".join(
        f"    {_action(t)}" for t in ("code", "run_coding_task")
    )

    return [
        {
            "name": "opencode_readonly_deny_coding",
            "description": "Deny run_coding_task for readonly role",
            "statement": _readonly_forbid("run_coding_task", gateway_arn),
        },
        {
            "name": "opencode_readonly_deny_cancel",
            "description": "Deny cancel_task for readonly role",
            "statement": _readonly_forbid("cancel_task", gateway_arn),
        },
        {
            "name": "opencode_readonly_deny_code",
            "description": "Deny code for readonly role",
            "statement": _readonly_forbid("code", gateway_arn),
        },
        {
            "name": "opencode_deny_production_repos",
            "description": "Deny coding tools on repositories whose name ends in -production",
            "statement": (
                "forbid(\n"
                "  principal,\n"
                "  action in [\n"
                f"{production_actions}\n"
                "  ],\n"
                f"  resource == {resource}\n"
                ") when {\n"
                f"{_production_condition()}\n"
                "};"
            ),
        },
        {
            "name": "opencode_permit_full_access",
            "description": (
                "Permit admin and developer roles to call the six opencode "
                "tools (forbid policies still apply)"
            ),
            "statement": _role_permit(TOOL_NAMES, FULL_ACCESS_ROLES, gateway_arn),
        },
        {
            "name": "opencode_permit_readonly_status",
            "description": "Permit readonly role to call get_task_status and list_tasks",
            "statement": _role_permit(
                READONLY_PERMITTED_TOOLS, (READONLY_ROLE,), gateway_arn
            ),
        },
    ]


def _get_stack_outputs(cfn_client, stack_name: str) -> dict[str, str]:
    """Return {OutputKey: OutputValue} for a CloudFormation stack."""
    resp = cfn_client.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def _find_policy(client, engine_id: str, name: str) -> dict | None:
    """Return the policy summary with the given name, or None.

    Policies that are failed or being deleted are ignored (failed ones are
    removed by ``_cleanup_failed`` before policies are applied).
    """
    paginator = client.get_paginator("list_policies")
    for page in paginator.paginate(policyEngineId=engine_id):
        for policy in page.get("policies", []):
            status = policy.get("status", "")
            if "FAILED" in status or status == "DELETING":
                continue
            if policy.get("name") == name:
                return policy
    return None


def _cleanup_failed(client, engine_id: str) -> None:
    """Delete any policies in FAILED state."""
    paginator = client.get_paginator("list_policies")
    for page in paginator.paginate(policyEngineId=engine_id):
        for policy in page.get("policies", []):
            if "FAILED" in policy.get("status", ""):
                print(f"  Deleting failed policy: {policy['name']} ({policy['policyId']})")
                client.delete_policy(policyEngineId=engine_id, policyId=policy["policyId"])
                time.sleep(1)


# Statuses a policy passes through on its way to ACTIVE. Anything else that
# is not ACTIVE (a *_FAILED status, DELETING, or an unrecognized value) is
# an error for a policy this script is applying.
_TRANSITIONAL_STATUSES = ("CREATING", "UPDATING")


def _check_status(p: dict, name: str) -> bool:
    """Return True if ACTIVE, False if transitional; raise otherwise."""
    status = p.get("status", "")
    if status == "ACTIVE":
        return True
    if status in _TRANSITIONAL_STATUSES:
        return False
    if "FAILED" in status:
        reasons = p.get("statusReasons", ["unknown"])
        raise RuntimeError(f"Policy '{name}' FAILED: {reasons}")
    raise RuntimeError(f"Policy '{name}' is {status or 'missing a status'}, expected ACTIVE")


def _wait_for_active(client, engine_id: str, policy_id: str, name: str) -> dict:
    """Poll a policy until ACTIVE; raise on FAILED or after 60s."""
    for _ in range(30):
        time.sleep(2)
        p = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
        if _check_status(p, name):
            print(f"  Policy '{name}' is ACTIVE.")
            return p
    raise TimeoutError(f"Policy '{name}' did not become ACTIVE within 60s")


def _get_active_policy(client, engine_id: str, policy_id: str, name: str) -> dict:
    """Fetch the policy now and return it once ACTIVE.

    The status of this fresh GetPolicy response is what counts, not the
    (possibly stale) ListPolicies summary: a transitional status is waited
    on, and any other non-ACTIVE status raises.
    """
    p = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
    if _check_status(p, name):
        return p
    print(f"  Policy '{name}' is {p.get('status')}, waiting for ACTIVE...")
    return _wait_for_active(client, engine_id, policy_id, name)


def _normalize_statement(statement: str) -> str:
    """Collapse whitespace so formatting differences do not trigger updates."""
    return " ".join(statement.split())


def _ensure_policy(client, engine_id: str, name: str, statement: str, description: str) -> str:
    """Create the policy, update it if its statement changed, or skip it.

    Returns the policy id. Returns only once a fresh GetPolicy response
    reports the policy ACTIVE; raises otherwise.
    """
    definition = {"cedar": {"statement": statement}}
    existing = _find_policy(client, engine_id, name)

    if existing is None:
        resp = client.create_policy(
            policyEngineId=engine_id,
            name=name,
            description=description,
            validationMode=VALIDATION_MODE,
            definition=definition,
        )
        policy_id = resp["policyId"]
        print(f"  Created policy '{name}' (id={policy_id}), waiting for ACTIVE...")
        _wait_for_active(client, engine_id, policy_id, name)
        return policy_id

    policy_id = existing["policyId"]
    # Gate on the status of the fresh GetPolicy response, not the list
    # summary: an ACTIVE summary can be stale.
    current = _get_active_policy(client, engine_id, policy_id, name)
    current_statement = current.get("definition", {}).get("cedar", {}).get("statement", "")
    if _normalize_statement(current_statement) == _normalize_statement(statement):
        print(f"  Policy '{name}' already ACTIVE and up to date - skipping.")
        return policy_id

    print(f"  Policy '{name}' statement differs - updating (id={policy_id})...")
    client.update_policy(
        policyEngineId=engine_id,
        policyId=policy_id,
        description=description,
        definition=definition,
        validationMode=VALIDATION_MODE,
    )
    _wait_for_active(client, engine_id, policy_id, name)
    return policy_id


def _wait_for_deleted(client, engine_id: str, policy_id: str, name: str) -> None:
    """Poll until GetPolicy raises ResourceNotFoundException, or 60s."""
    for _ in range(30):
        try:
            client.get_policy(policyEngineId=engine_id, policyId=policy_id)
        except client.exceptions.ResourceNotFoundException:
            print(f"  Policy '{name}' deleted.")
            return
        time.sleep(2)
    raise TimeoutError(f"Policy '{name}' was not deleted within 60s")


def delete_policies(client, engine_id: str, gateway_arn: str) -> None:
    """Delete the policies this script manages (by exact name); idempotent."""
    managed = [p["name"] for p in build_policies(gateway_arn)]
    by_name: dict[str, list[dict]] = {}
    paginator = client.get_paginator("list_policies")
    for page in paginator.paginate(policyEngineId=engine_id):
        for policy in page.get("policies", []):
            if policy.get("name") in managed and policy.get("status") != "DELETING":
                by_name.setdefault(policy["name"], []).append(policy)

    for name in managed:
        if name not in by_name:
            print(f"  Policy '{name}' not found - skipping.")
            continue
        for policy in by_name[name]:
            print(f"  Deleting policy '{name}' (id={policy['policyId']})...")
            client.delete_policy(policyEngineId=engine_id, policyId=policy["policyId"])
            _wait_for_deleted(client, engine_id, policy["policyId"], name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Cedar policies post-deploy")
    parser.add_argument("--region", required=True)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the six managed policies instead of creating them "
             "(run before `cdk destroy`; the policy engine cannot be deleted "
             "while it contains policies).",
    )
    args = parser.parse_args()

    cfn = boto3.client("cloudformation", region_name=args.region)
    agentcore = boto3.client("bedrock-agentcore-control", region_name=args.region)

    outputs = _get_stack_outputs(cfn, "OpenCodeGateway")
    engine_id = outputs["PolicyEngineId"]
    gateway_arn = outputs["GatewayArn"]

    print(f"PolicyEngine: {engine_id}")
    print(f"Gateway ARN:  {gateway_arn}")

    if args.delete:
        print("\nDeleting Cedar policies...")
        delete_policies(agentcore, engine_id, gateway_arn)
        print("\nAll managed policies deleted.")
        return

    # Clean up any failed policies from previous attempts
    print("\nCleaning up failed policies...")
    _cleanup_failed(agentcore, engine_id)
    time.sleep(3)

    print("\nApplying Cedar policies...")
    for policy in build_policies(gateway_arn):
        _ensure_policy(agentcore, engine_id, **policy)

    print("\nAll policies applied successfully.")


if __name__ == "__main__":
    main()
