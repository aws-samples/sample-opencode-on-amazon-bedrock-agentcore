# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Gateway REQUEST interceptor (lambda/interceptor/index.py).

Covers caller-identity handling: base64url JWT payloads, auth scheme and
header-name casing, requests without a bearer token, claim typing,
tools/call body-shape validation, and removal of a client-supplied
``_user_id``.
"""

import base64
import importlib
import json

import pytest

# "lambda" is a Python keyword, so the module is imported by name.
handler = importlib.import_module("lambda.interceptor.index").handler


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(claims) -> str:
    header = _b64url(json.dumps({"alg": "none"}).encode())
    return f"{header}.{_b64url(json.dumps(claims).encode())}.sig"


def _event(headers=None, method="tools/call", arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "tools/call":
        body["params"] = {"name": "opencode___list_tasks", "arguments": arguments or {}}
    return {"mcp": {"gatewayRequest": {"headers": headers or {}, "body": body}}}


def _status(result):
    return result.get("mcp", {}).get("transformedGatewayResponse", {}).get("statusCode")


def _message(result):
    return result["mcp"]["transformedGatewayResponse"]["body"]["error"]["message"]


def _request(result):
    return result["mcp"]["transformedGatewayRequest"]


def _user_id(result):
    return _request(result)["body"]["params"]["arguments"].get("_user_id")


class TestBase64UrlPayload:
    def test_payload_with_url_safe_characters_decodes(self):
        # {"sub": "???>>>"} encodes to a base64url payload containing both
        # "-" and "_", which standard base64 decoding does not accept.
        sub = "???>>>"
        payload = _b64url(json.dumps({"sub": sub}).encode())
        assert "-" in payload and "_" in payload
        result = handler(_event({"Authorization": f"Bearer aaa.{payload}.sig"}), None)
        assert _status(result) is None
        assert _user_id(result) == sub


class TestAuthHeaderParsing:
    def test_lowercase_bearer_scheme_accepted(self):
        result = handler(_event({"Authorization": f"bearer {_jwt({'sub': 'u-1'})}"}), None)
        assert _user_id(result) == "u-1"

    @pytest.mark.parametrize("name", ["authorization", "AUTHORIZATION", "AuThOrIzAtIoN"])
    def test_header_name_matched_case_insensitively(self, name):
        result = handler(_event({name: f"Bearer {_jwt({'sub': 'u-2'})}"}), None)
        assert _user_id(result) == "u-2"
        assert all(k.lower() != "authorization" for k in _request(result)["headers"])

    def test_authorization_header_not_forwarded(self):
        headers = {"Authorization": f"Bearer {_jwt({'sub': 'u-3'})}", "Mcp-Session-Id": "s-1"}
        result = handler(_event(headers), None)
        assert _request(result)["headers"] == {"Mcp-Session-Id": "s-1"}

    def test_email_claim_is_not_an_identity(self):
        result = handler(_event({"Authorization": f"Bearer {_jwt({'email': 'a@example.com'})}"}), None)
        assert _status(result) == 401
        assert _message(result) == "Missing sub in JWT"


class TestClaimTyping:
    @pytest.mark.parametrize("sub", ["", 123, None, ["u-1"], {"id": "u-1"}])
    def test_non_string_or_empty_sub_rejected(self, sub):
        result = handler(_event({"Authorization": f"Bearer {_jwt({'sub': sub})}"}), None)
        assert _status(result) == 401
        assert _message(result) == "Missing sub in JWT"

    @pytest.mark.parametrize("payload", [["sub", "u-1"], "u-1", 42, None])
    def test_non_object_payload_rejected(self, payload):
        result = handler(_event({"Authorization": f"Bearer {_jwt(payload)}"}), None)
        assert _status(result) == 401
        assert _message(result) == "JWT decode failed"


class TestMissingIdentity:
    def test_tool_call_without_header_rejected(self):
        result = handler(_event({}), None)
        assert _status(result) == 401
        assert _message(result) == "Missing bearer token"
        assert "transformedGatewayRequest" not in result["mcp"]

    def test_tool_call_with_non_bearer_scheme_rejected(self):
        result = handler(_event({"Authorization": "Basic abc123"}), None)
        assert _status(result) == 401
        assert _message(result) == "Missing bearer token"

    def test_tool_call_with_empty_bearer_rejected(self):
        result = handler(_event({"Authorization": "Bearer "}), None)
        assert _status(result) == 401

    def test_tool_call_with_undecodable_token_rejected(self):
        result = handler(_event({"Authorization": "Bearer not-a-jwt"}), None)
        assert _status(result) == 401
        assert _message(result) == "JWT decode failed"

    def test_tool_call_without_header_and_client_user_id_rejected(self):
        result = handler(_event({}, arguments={"_user_id": "someone-else"}), None)
        assert _status(result) == 401
        assert "someone-else" not in json.dumps(result)

    def test_non_tool_call_without_header_passes_through(self):
        event = _event({"Mcp-Session-Id": "s-1"}, method="tools/list")
        result = handler(event, None)
        assert _status(result) is None
        assert _request(result)["body"] == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


class TestClientUserIdReplaced:
    def test_client_user_id_is_overwritten(self):
        event = _event(
            {"Authorization": f"Bearer {_jwt({'sub': 'real-sub'})}"},
            arguments={"_user_id": "someone-else", "repo": "r"},
        )
        result = handler(event, None)
        args = _request(result)["body"]["params"]["arguments"]
        assert args == {"_user_id": "real-sub", "repo": "r"}

    def test_tool_call_without_arguments_gets_user_id(self):
        event = {"mcp": {"gatewayRequest": {
            "headers": {"Authorization": f"Bearer {_jwt({'sub': 'u-4'})}"},
            "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t"}},
        }}}
        assert _user_id(handler(event, None)) == "u-4"

    def test_tool_call_without_params_gets_user_id(self):
        event = {"mcp": {"gatewayRequest": {
            "headers": {"Authorization": f"Bearer {_jwt({'sub': 'u-6'})}"},
            "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        }}}
        assert _user_id(handler(event, None)) == "u-6"


class TestBodyShapeValidation:
    @pytest.mark.parametrize("arguments", [["_user_id", "x"], None, "x", 7])
    def test_non_dict_arguments_rejected(self, arguments):
        event = _event({"Authorization": f"Bearer {_jwt({'sub': 'u-5'})}"})
        event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = arguments
        result = handler(event, None)
        assert _status(result) == 400
        assert _message(result) == "Invalid tools/call arguments"

    @pytest.mark.parametrize("params", [["x"], None, "x", 7])
    def test_non_dict_params_rejected(self, params):
        event = _event({"Authorization": f"Bearer {_jwt({'sub': 'u-5'})}"})
        event["mcp"]["gatewayRequest"]["body"]["params"] = params
        result = handler(event, None)
        assert _status(result) == 400
        assert _message(result) == "Invalid tools/call params"
