# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Gateway REQUEST interceptor — extracts user_id from JWT and injects into tool arguments."""

import base64
import json


def _strip_authorization(headers):
    """Return a copy of headers with any Authorization header removed.

    The inbound Authorization header (Cognito JWT) must never be forwarded.
    Any headers returned in transformedGatewayRequest.headers are forwarded
    verbatim to the target (see interceptor header propagation:
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html#gateway-headers-interceptor-propagation).
    If the inbound "Authorization: Bearer <cognito-jwt>" were returned here,
    it would replace the Gateway's outbound SigV4 Authorization header,
    causing a signature mismatch at the Runtime.
    """
    return {k: v for k, v in headers.items() if k.lower() != "authorization"}


def _strip_client_user_id(body):
    """Remove any client-supplied _user_id from tools/call arguments.

    ``_user_id`` is always set from the ``sub`` claim of the JWT the Gateway
    has already validated, so a value supplied by the client is discarded.
    """
    if body.get("method") == "tools/call" and isinstance(body.get("params"), dict):
        args = body["params"].get("arguments")
        if isinstance(args, dict):
            args.pop("_user_id", None)


def _error_response(status_code, message):
    """Build an interceptor response that short-circuits the request."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": status_code,
                "body": {"jsonrpc": "2.0", "error": {"code": -32600, "message": message}},
            }
        },
    }


def _bearer_token(headers):
    """Return the bearer token from the Authorization header, or None.

    The header name and the auth scheme are both matched case-insensitively
    (RFC 9110 sections 5.1 and 11.1).
    """
    auth = next(
        (v for k, v in headers.items() if k.lower() == "authorization"), ""
    )
    if not isinstance(auth, str):
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _decode_claims(token):
    """Decode the JWT payload segment into a claims dict.

    JWTs use unpadded base64url (RFC 7515 section 2), so `=` padding is
    added before decoding. Raises ``ValueError`` (or a decode error) when
    the payload is missing, undecodable, or not a JSON object.
    """
    segments = token.split(".")
    if len(segments) < 2 or not segments[1]:
        raise ValueError("JWT has no payload segment")
    payload = segments[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not a JSON object")
    return claims


def _passthrough(headers, body):
    """Forward the request without identity injection."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": _strip_authorization(headers),
                "body": body,
            }
        },
    }


def handler(event, context):
    mcp = event.get("mcp", {})
    gw_req = mcp.get("gatewayRequest", {})
    headers = gw_req.get("headers", {})
    body = gw_req.get("body", {})

    is_tools_call = body.get("method") == "tools/call"

    # _user_id is set from the JWT below; drop any value the client sent.
    _strip_client_user_id(body)

    # Extract the user id from the JWT (no verification needed - the Gateway
    # already validated the token).
    token = _bearer_token(headers)
    if token is None:
        if is_tools_call:
            # No token means no identity to attribute the call to; reject
            # rather than forward an anonymous tools/call.
            return _error_response(401, "Missing bearer token")
        # Internal Gateway calls (e.g., policy validation, tool discovery)
        # may not carry a Cognito JWT.  Pass them through without user
        # injection.
        return _passthrough(headers, body)

    try:
        claims = _decode_claims(token)
    except Exception:
        if is_tools_call:
            return _error_response(401, "JWT decode failed")
        return _passthrough(headers, body)

    # Identity is derived from `sub` only: the stable, unique Cognito user
    # identifier. It must be a non-empty string.
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        if is_tools_call:
            return _error_response(401, "Missing sub in JWT")
        return _passthrough(headers, body)

    # Inject the server-derived user_id into tool call arguments. Missing
    # params/arguments are created so every forwarded tools/call carries an
    # identity; a params or arguments value that is not an object is
    # rejected instead of being forwarded.
    if is_tools_call:
        params = body.setdefault("params", {})
        if not isinstance(params, dict):
            return _error_response(400, "Invalid tools/call params")
        args = params.setdefault("arguments", {})
        if not isinstance(args, dict):
            return _error_response(400, "Invalid tools/call arguments")
        args["_user_id"] = user_id

    return _passthrough(headers, body)
