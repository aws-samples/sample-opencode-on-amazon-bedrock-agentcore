# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: OAuth Callback Authorizer & URL Discovery.

Tests the inline authorizer Lambda logic (AUTHORIZER_LAMBDA_CODE), the
callback Lambda handler, and that ``resolve_git_credential`` passes the
``OAUTH_CALLBACK_URL`` env var through as the OAuth2 return URL.

Uses Hypothesis for property-based testing.
"""

from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Extract the authorizer handler from the inline code string in callback_api_stack
# ---------------------------------------------------------------------------
from stacks.callback_api_stack import AUTHORIZER_LAMBDA_CODE

_authorizer_module = types.ModuleType("authorizer_inline")
exec(AUTHORIZER_LAMBDA_CODE, _authorizer_module.__dict__)  # noqa: S102
authorizer_handler = _authorizer_module.handler


# ---------------------------------------------------------------------------
# Load the OAuth callback handler from lambda/oauth_callback/index.py.
# "lambda" is a Python keyword, so the directory cannot be imported as a
# package — load it directly from its file path.
# ---------------------------------------------------------------------------
_CALLBACK_INDEX = (
    Path(__file__).resolve().parents[2] / "lambda" / "oauth_callback" / "index.py"
)
_callback_spec = importlib.util.spec_from_file_location(
    "oauth_callback_index_prop", _CALLBACK_INDEX
)
oauth_callback = importlib.util.module_from_spec(_callback_spec)
_callback_spec.loader.exec_module(oauth_callback)


from unittest.mock import MagicMock

# The package re-exports the function under the same name as the submodule,
# so fetch the module object explicitly.
_resolve_mod = importlib.import_module("container.tools.resolve_git_credential")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty text for valid session_id / state values (old permissive authorizer)
nonempty_text = st.text(min_size=1, max_size=200)

# Valid session_id: 10-512 chars from [A-Za-z0-9_\-/:.] per the hardened authorizer
valid_session_id = st.from_regex(r"[A-Za-z0-9_\-/:.]{10,100}", fullmatch=True)

# Valid state: JSON dict containing at least "user_id"
import json as _json

valid_state = st.fixed_dictionaries(
    {"user_id": st.text(min_size=1, max_size=50)},
    optional={"extra": st.text(max_size=20)},
).map(_json.dumps)

# Possibly-empty text (includes empty string)
any_text = st.text(max_size=200)

# URL-like strings for OAUTH_CALLBACK_URL (no null bytes or surrogates — invalid in env vars)
url_text = st.text(
    alphabet=st.characters(
        blacklist_characters="\x00",
        blacklist_categories=("Cs",),  # exclude surrogates
    ),
    min_size=1,
    max_size=500,
)


# ---------------------------------------------------------------------------
# Property 1: Authorizer accepts valid OAuth callbacks
# ---------------------------------------------------------------------------


class TestAuthorizerAcceptsValid:
    """Property 1: Authorizer accepts valid OAuth callbacks.

    **Validates: Requirements 2.2**

    For any valid session_id (10-512 chars, allowed charset) and valid
    state (JSON dict with user_id), the authorizer SHALL return
    isAuthorized: True.
    """

    @given(session_id=valid_session_id, state=valid_state)
    @settings(max_examples=50, deadline=5_000)
    def test_valid_session_id_and_state_returns_authorized(
        self, session_id: str, state: str
    ):
        """Valid session_id + valid state JSON → isAuthorized: True."""
        event = {
            "queryStringParameters": {
                "session_id": session_id,
                "state": state,
            }
        }
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": True}, (
            f"Expected isAuthorized=True for session_id={session_id!r}, "
            f"state={state!r}, got {result}"
        )


# ---------------------------------------------------------------------------
# Property 2: Authorizer rejects malformed requests
# ---------------------------------------------------------------------------


class TestAuthorizerRejectsMalformed:
    """Property 2: Authorizer rejects malformed requests.

    **Validates: Requirements 2.3**

    For any request where session_id is missing/empty OR state is
    missing/empty, the authorizer Lambda SHALL return isAuthorized: False.
    """

    @given(state=nonempty_text)
    @settings(max_examples=30, deadline=5_000)
    def test_missing_session_id_returns_unauthorized(self, state: str):
        """Missing session_id (key absent) → isAuthorized: False."""
        event = {"queryStringParameters": {"state": state}}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}, (
            f"Expected isAuthorized=False when session_id missing, got {result}"
        )

    @given(state=nonempty_text)
    @settings(max_examples=30, deadline=5_000)
    def test_empty_session_id_returns_unauthorized(self, state: str):
        """Empty session_id → isAuthorized: False."""
        event = {"queryStringParameters": {"session_id": "", "state": state}}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}, (
            f"Expected isAuthorized=False for empty session_id, got {result}"
        )

    @given(session_id=nonempty_text)
    @settings(max_examples=30, deadline=5_000)
    def test_missing_state_returns_unauthorized(self, session_id: str):
        """Missing state (key absent) → isAuthorized: False."""
        event = {"queryStringParameters": {"session_id": session_id}}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}, (
            f"Expected isAuthorized=False when state missing, got {result}"
        )

    @given(session_id=nonempty_text)
    @settings(max_examples=30, deadline=5_000)
    def test_empty_state_returns_unauthorized(self, session_id: str):
        """Empty state → isAuthorized: False."""
        event = {"queryStringParameters": {"session_id": session_id, "state": ""}}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}, (
            f"Expected isAuthorized=False for empty state, got {result}"
        )

    @settings(max_examples=1, deadline=5_000)
    @given(st.just(None))
    def test_both_missing_returns_unauthorized(self, _):
        """Both session_id and state missing → isAuthorized: False."""
        event = {"queryStringParameters": {}}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}

    @settings(max_examples=1, deadline=5_000)
    @given(st.just(None))
    def test_null_query_params_returns_unauthorized(self, _):
        """queryStringParameters is None → isAuthorized: False."""
        event = {"queryStringParameters": None}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}

    @settings(max_examples=1, deadline=5_000)
    @given(st.just(None))
    def test_no_query_params_key_returns_unauthorized(self, _):
        """queryStringParameters key absent → isAuthorized: False."""
        event = {}
        result = authorizer_handler(event, None)
        assert result == {"isAuthorized": False}


# ---------------------------------------------------------------------------
# Property 3: Callback URL discovery returns environment variable
# ---------------------------------------------------------------------------


class TestCallbackUrlDiscovery:
    """Property 3: the OAuth2 return URL comes from OAUTH_CALLBACK_URL.

    **Validates: Requirements 5.1, 5.2**

    For any URL string set as OAUTH_CALLBACK_URL, ``resolve_git_credential``
    passes it as ``resourceOauth2ReturnUrl`` (with ``customState`` carrying
    the user id). When the env var is unset, neither parameter is sent.
    """

    @staticmethod
    def _client():
        client = MagicMock()
        client.get_workload_access_token_for_user_id.return_value = {
            "workloadAccessToken": "wat"
        }
        client.get_resource_oauth2_token.return_value = {"accessToken": "tok"}
        return client

    @given(url=url_text)
    @settings(max_examples=50, deadline=5_000)
    def test_resolve_git_credential_passes_env_var_as_return_url(self, url: str):
        client = self._client()
        with patch.dict(os.environ, {"OAUTH_CALLBACK_URL": url}), \
                patch.object(_resolve_mod, "_get_client", return_value=client):
            _resolve_mod.resolve_git_credential(
                user_id="u-1", repo_url="https://github.com/o/r"
            )
        params = client.get_resource_oauth2_token.call_args.kwargs
        assert params["resourceOauth2ReturnUrl"] == url
        assert '"u-1"' in params["customState"]

    def test_resolve_git_credential_omits_return_url_when_unset(self):
        client = self._client()
        env = os.environ.copy()
        env.pop("OAUTH_CALLBACK_URL", None)
        with patch.dict(os.environ, env, clear=True), \
                patch.object(_resolve_mod, "_get_client", return_value=client):
            _resolve_mod.resolve_git_credential(
                user_id="u-1", repo_url="https://github.com/o/r"
            )
        params = client.get_resource_oauth2_token.call_args.kwargs
        assert "resourceOauth2ReturnUrl" not in params
        assert "customState" not in params


# ---------------------------------------------------------------------------
# WI-7 Property 4: Callback HTML response never reflects raw markup
# ---------------------------------------------------------------------------

# Text that frequently contains HTML metacharacters we must escape.
html_payload = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=200,
)


class TestCallbackHtmlEscapingProperty:
    """Property: no raw HTML metacharacter survives into the response body.

    For any message string, ``_html`` SHALL NOT emit an unescaped ``<``,
    ``>``, ``&``, ``"`` or ``'`` originating from that message. We verify
    this by asserting the message's own characters are escaped: the body
    contains no raw ``<``/``>`` from the payload region.
    """

    @given(message=html_payload)
    @settings(max_examples=100, deadline=5_000)
    def test_message_metacharacters_are_escaped(self, message: str):
        import html as _html

        resp = oauth_callback._html(200, message)
        body = resp["body"]

        # Isolate the message region (the text the handler interpolated
        # into the <h2> element). Only that region reflects user content;
        # the surrounding static markup legitimately contains quotes etc.
        region = body.split("<h2>", 1)[1].split("</h2>", 1)[0]

        escaped = _html.escape(message, quote=True)
        assert region == escaped, (
            f"Message region {region!r} is not the escaped payload "
            f"{escaped!r} for input {message!r}"
        )

        # No raw metacharacter from the message survives into the region.
        for raw, entity in (("<", "&lt;"), (">", "&gt;")):
            if raw in message:
                assert raw not in region, (
                    f"Raw {raw!r} leaked into message region: {region!r}"
                )
                assert entity in region

    @given(message=html_payload)
    @settings(max_examples=50, deadline=5_000)
    def test_security_headers_always_present(self, message: str):
        resp = oauth_callback._html(200, message)
        headers = resp["headers"]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"


# ---------------------------------------------------------------------------
# WI-7 Property 5: Authorizer never logs raw session_id / state
# ---------------------------------------------------------------------------


class TestAuthorizerLoggingHygieneProperty:
    """Property: the authorizer's stdout never contains raw session_id/state."""

    @given(session_id=valid_session_id, state=valid_state)
    @settings(max_examples=50, deadline=5_000)
    def test_valid_request_logs_no_raw_values(self, session_id, state):
        import contextlib
        import io

        buf = io.StringIO()
        event = {"queryStringParameters": {"session_id": session_id, "state": state}}
        with contextlib.redirect_stdout(buf):
            result = authorizer_handler(event, None)
        out = buf.getvalue()

        assert result == {"isAuthorized": True}
        assert session_id not in out
        assert state not in out


# ---------------------------------------------------------------------------
# WI-7 Property 6: Callback Lambda keeps OAuth flow values out of logs on the
# success and HTTPError paths (AWS calls stubbed so both branches run).
# ---------------------------------------------------------------------------

import html as _html_mod
import io as _io
import urllib.error as _urllib_error
from unittest.mock import MagicMock as _MagicMock

# Marker-prefixed values, so a match in log output can only come from the input.
_marker_session_id = st.from_regex(r"SID[A-Za-z0-9]{8,40}", fullmatch=True)
_marker_user_id = st.from_regex(r"UID[A-Za-z0-9]{8,40}", fullmatch=True)

# Text that always contains at least one character html.escape rewrites.
_markup_text = st.builds(
    lambda a, ch, b: a + ch + b,
    st.text(max_size=30),
    st.sampled_from(list("<>&\"'")),
    st.text(max_size=30),
)


def _fake_botocore_session():
    frozen = _MagicMock(access_key="AKIDEXAMPLE", secret_key="example", token=None)
    creds = _MagicMock()
    creds.get_frozen_credentials.return_value = frozen
    session = _MagicMock()
    session.get_credentials.return_value = creds
    return session


def _run_callback(event, urlopen_kwargs):
    """Run the callback handler with AWS calls stubbed; return (result, log)."""
    out = _io.StringIO()

    def _capture(*args, **_kwargs):
        out.write(" ".join(str(a) for a in args) + "\n")

    with patch.object(oauth_callback.botocore.session, "get_session",
                      return_value=_fake_botocore_session()), \
         patch.object(oauth_callback.urllib.request, "urlopen", **urlopen_kwargs), \
         patch("builtins.print", side_effect=_capture):
        result = oauth_callback.handler(event, None)
    return result, out.getvalue()


def _callback_event(session_id, user_id):
    return {"queryStringParameters": {
        "session_id": session_id,
        "state": _json.dumps({"user_id": user_id}),
    }}


class TestCallbackLambdaHygiene:
    """Logging properties of lambda/oauth_callback/index.py."""

    @given(session_id=_marker_session_id, user_id=_marker_user_id)
    @settings(max_examples=30, deadline=5_000)
    def test_success_path_does_not_log_flow_values(self, session_id, user_id):
        resp = _MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"marker":"RESPONSEMARKER"}'
        resp.__enter__.return_value = resp
        result, logged = _run_callback(
            _callback_event(session_id, user_id), {"return_value": resp},
        )
        assert result["statusCode"] == 200
        assert session_id not in logged
        assert user_id not in logged
        assert "RESPONSEMARKER" not in logged

    @given(session_id=_marker_session_id, user_id=_marker_user_id, payload=_markup_text)
    @settings(max_examples=30, deadline=5_000)
    def test_error_body_is_escaped_and_not_logged(self, session_id, user_id, payload):
        error_body = f"{session_id} {user_id} {payload}"
        http_error = _urllib_error.HTTPError(
            url="https://example.invalid", code=400, msg="Bad Request",
            hdrs={"x-amzn-RequestId": "req-1"}, fp=_io.BytesIO(error_body.encode()),
        )
        result, logged = _run_callback(
            _callback_event(session_id, user_id), {"side_effect": http_error},
        )
        assert result["statusCode"] == 400
        assert session_id not in logged
        assert user_id not in logged
        assert "HTTP 400" in logged
        assert "req-1" in logged
        assert _html_mod.escape(error_body, quote=True) in result["body"]
