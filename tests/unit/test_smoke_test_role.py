# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the smoke-test user's role handling in scripts/smoke-test.py.

The Cedar permits are role-gated, so the smoke-test user needs a role to be
permitted under ENFORCE. The script sets ``custom:role=developer`` only when
the user has no role, and leaves an existing role unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "smoke-test.py"


@pytest.fixture()
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_test_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations via sys.modules[__module__].
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _cognito(attrs: dict) -> MagicMock:
    client = MagicMock()
    client.admin_get_user.return_value = {
        "Username": "u",
        "UserAttributes": [{"Name": k, "Value": v} for k, v in attrs.items()],
    }
    client.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": "id-token"}}
    return client


class TestEnsureUserRole:
    def test_role_set_when_missing(self, smoke):
        client = _cognito({"email": "user@example.com", "sub": "abc"})
        smoke.ensure_user_role(client, "pool-1", "user@example.com")
        client.admin_update_user_attributes.assert_called_once_with(
            UserPoolId="pool-1",
            Username="user@example.com",
            UserAttributes=[{"Name": "custom:role", "Value": "developer"}],
        )

    def test_empty_role_treated_as_missing(self, smoke):
        client = _cognito({"custom:role": ""})
        smoke.ensure_user_role(client, "pool-1", "u")
        client.admin_update_user_attributes.assert_called_once()

    @pytest.mark.parametrize("role", ["admin", "developer"])
    def test_existing_full_access_role_left_unchanged(self, smoke, role, capsys):
        client = _cognito({"custom:role": role})
        smoke.ensure_user_role(client, "pool-1", "u")
        client.admin_update_user_attributes.assert_not_called()
        assert "WARNING" not in capsys.readouterr().out

    @pytest.mark.parametrize("role", ["readonly", "guest"])
    def test_other_role_left_unchanged_with_warning(self, smoke, role, capsys):
        client = _cognito({"custom:role": role})
        smoke.ensure_user_role(client, "pool-1", "u")
        client.admin_update_user_attributes.assert_not_called()
        assert "WARNING" in capsys.readouterr().out


class TestAcquireJwtOrder:
    def test_role_set_before_authentication(self, smoke):
        client = _cognito({})
        session = MagicMock()
        session.client.return_value = client

        token = smoke.acquire_cognito_jwt(session, "pool-1", "client-1", "u")

        assert token == "id-token"
        names = [c[0] for c in client.mock_calls]
        assert names.index("admin_update_user_attributes") < names.index("initiate_auth")
