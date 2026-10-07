# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Logging hygiene tests for the OAuth callback handler and the callback
API authorizer: no sensitive values are logged, HTML output is escaped, and
security headers are set.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Load the OAuth callback handler from lambda/oauth_callback/index.py.
# "lambda" is a Python keyword so the directory cannot be imported as a
# normal package — load it directly from its file path.
# ---------------------------------------------------------------------------
_CALLBACK_INDEX = (
    Path(__file__).resolve().parents[2] / "lambda" / "oauth_callback" / "index.py"
)
_spec = importlib.util.spec_from_file_location("oauth_callback_index", _CALLBACK_INDEX)
oauth_callback = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oauth_callback)


# ---------------------------------------------------------------------------
# WI-7: OAuth callback handler logging hygiene (no sensitive values logged)
# ---------------------------------------------------------------------------

_SENSITIVE_SESSION_ID = "sess-SECRET-0123456789abcdef"
_SENSITIVE_USER_ID = "user-SECRET-abc"
_SENSITIVE_STATE = json.dumps({"user_id": _SENSITIVE_USER_ID})


def _invoke_callback_capture_prints(event, capsys):
    """Invoke the callback handler and return (result, captured_stdout).

    The handler logs via ``print`` so stdout capture is the source of
    truth for what would land in CloudWatch logs.
    """
    result = oauth_callback.handler(event, None)
    captured = capsys.readouterr()
    return result, captured.out


class TestCallbackHandlerDoesNotLogSensitiveValues:
    """The callback handler must not print session_id, state, or raw params."""

    def test_missing_session_id_does_not_log_state_value(self, capsys):
        # No session_id → early return, but state is present in query params.
        event = {"queryStringParameters": {"state": _SENSITIVE_STATE}}
        _result, out = _invoke_callback_capture_prints(event, capsys)

        assert _SENSITIVE_STATE not in out
        assert _SENSITIVE_USER_ID not in out
        # Presence indicators are fine.
        assert "state_present=True" in out

    def test_valid_params_do_not_log_session_id_or_state(self, capsys):
        event = {
            "queryStringParameters": {
                "session_id": _SENSITIVE_SESSION_ID,
                "state": _SENSITIVE_STATE,
            }
        }

        # Force the SigV4 / upstream call to fail fast so we only exercise
        # the request-handling log path (no network).
        with patch.object(
            oauth_callback.botocore.session,
            "get_session",
            side_effect=RuntimeError("no creds in test"),
        ):
            _result, out = _invoke_callback_capture_prints(event, capsys)

        assert _SENSITIVE_SESSION_ID not in out
        assert _SENSITIVE_STATE not in out
        assert _SENSITIVE_USER_ID not in out
        assert "session_id_present=True" in out
        assert "state_present=True" in out


class TestCallbackHandlerHtmlEscaping:
    """Interpolated values in the HTML response body must be html.escape'd."""

    _INJECTION = "<script>alert('xss')</script> & \"quotes\""

    def test_injected_value_is_escaped_in_response_body(self):
        # The injection reaches the body via the error message when the
        # user identity is derived from a non-JSON state string.
        event = {
            "queryStringParameters": {
                "session_id": _SENSITIVE_SESSION_ID,
                "state": self._INJECTION,
            }
        }
        # state is not JSON → user_id = state (the raw injection), then the
        # upstream call fails and the injection would be reflected into HTML.
        with patch.object(
            oauth_callback.botocore.session,
            "get_session",
            side_effect=RuntimeError(self._INJECTION),
        ):
            result = oauth_callback.handler(event, None)

        body = result["body"]
        # Raw markup must not appear; escaped entities must.
        assert "<script>" not in body
        assert "&lt;script&gt;" in body
        assert "&amp;" in body
        assert "&quot;" in body or "&#x27;" in body

    def test_html_helper_escapes_directly(self):
        resp = oauth_callback._html(400, "<b>bad</b> & 'x' \"y\"")
        body = resp["body"]
        assert "<b>bad</b>" not in body
        assert "&lt;b&gt;bad&lt;/b&gt;" in body


class TestCallbackHandlerSecurityHeaders:
    """The callback response must carry the hardening headers."""

    def test_headers_present_on_success_path(self):
        resp = oauth_callback._html(200, "ok")
        headers = resp["headers"]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"

    def test_headers_present_on_error_path(self):
        resp = oauth_callback._html(400, "Missing session_id parameter")
        headers = resp["headers"]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"


# ---------------------------------------------------------------------------
# WI-7: Inline authorizer logging hygiene (no sensitive values logged)
# ---------------------------------------------------------------------------

from stacks.callback_api_stack import AUTHORIZER_LAMBDA_CODE  # noqa: E402

_authorizer_ns: dict = {}
exec(AUTHORIZER_LAMBDA_CODE, _authorizer_ns)  # noqa: S102
_authorizer_handler = _authorizer_ns["handler"]


class TestAuthorizerDoesNotLogSensitiveValues:
    """The inline authorizer must not print session_id, state, or raw params."""

    def test_valid_request_does_not_log_session_id_or_state(self, capsys):
        event = {
            "queryStringParameters": {
                "session_id": _SENSITIVE_SESSION_ID,
                "state": _SENSITIVE_STATE,
            }
        }
        result = _authorizer_handler(event, None)
        out = capsys.readouterr().out

        assert result == {"isAuthorized": True}
        assert _SENSITIVE_SESSION_ID not in out
        assert _SENSITIVE_STATE not in out
        assert _SENSITIVE_USER_ID not in out
        assert "session_id_present=True" in out

    def test_invalid_state_does_not_log_parsed_value(self, capsys):
        secret = "SECRET-not-json-value"
        event = {
            "queryStringParameters": {
                "session_id": _SENSITIVE_SESSION_ID,
                "state": secret,
            }
        }
        result = _authorizer_handler(event, None)
        out = capsys.readouterr().out

        assert result == {"isAuthorized": False}
        assert secret not in out
        assert _SENSITIVE_SESSION_ID not in out
