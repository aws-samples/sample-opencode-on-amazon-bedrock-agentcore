# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OAuth2 callback handler for AgentCore Identity 3LO flow."""

import html
import json
import os
import urllib.request
import urllib.error
import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


REGION = os.environ.get("AWS_REGION", "us-east-1")


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    session_id = params.get("session_id", "")
    state = params.get("state", "")

    # Never log the raw session_id, state, or query params — they carry
    # sensitive OAuth values. Log only presence booleans.
    print(f"Callback received: session_id_present={bool(session_id)}, state_present={bool(state)}")

    if not session_id:
        return _html(400, "Missing session_id parameter")

    user_id = ""
    if state:
        try:
            state_data = json.loads(state)
            user_id = state_data.get("user_id", "")
        except (json.JSONDecodeError, TypeError):
            user_id = state

    if not user_id:
        return _html(400, "Missing user identity in state parameter")

    try:
        session = botocore.session.get_session()
        credentials = session.get_credentials().get_frozen_credentials()

        url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/identities/CompleteResourceTokenAuth"
        body = json.dumps({
            "sessionUri": session_id,
            "userIdentifier": {"userId": user_id},
        })

        print(f"Calling {url}")

        aws_request = AWSRequest(method="POST", url=url, data=body, headers={
            "Content-Type": "application/json",
        })
        SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(aws_request)

        req = urllib.request.Request(url, data=body.encode(), method="POST")
        for key, val in aws_request.headers.items():
            req.add_header(key, val)

        with urllib.request.urlopen(req) as resp:
            resp.read()
            print(f"Success: {resp.status}")

    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else "no body"
        # Log only the status and request id: the upstream error body can
        # echo request values (e.g. the session URI or user id).
        request_id = e.headers.get("x-amzn-RequestId", "") if e.headers else ""
        print(f"HTTP {e.code} from CompleteResourceTokenAuth, request_id={request_id}")
        return _html(e.code, f"Authorization failed: HTTP {e.code} — {error_body}")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
        return _html(500, f"Authorization failed: {e}")

    return _html(200, "Authorization complete. You can close this tab and return to your MCP client.")


def _html(status_code, message):
    # Escape any interpolated value so reflected content (e.g. upstream
    # error bodies) cannot inject markup into the response.
    safe_message = html.escape(str(message), quote=True)
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        "body": f"""<html>
<head><meta charset="utf-8"><title>OpenCode on AgentCore</title></head>
<body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;">
<div style="text-align:center;"><h2>{safe_message}</h2></div>
</body></html>""",
    }
