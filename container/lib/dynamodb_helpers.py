# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lightweight DynamoDB job history/audit records — NOT a state machine.

Provides helpers for writing, updating, and querying job records in the
opencode-jobs DynamoDB table.  Only 4 valid states: RUNNING, COMPLETE,
FAILED, CANCELLED.

Key schema:
    PK = user#{user_id}
    SK = job#{job_id}#{created_at_iso}
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
JOB_TABLE = os.environ.get("JOB_TABLE_NAME", "opencode-jobs")
VALID_STATES = {"RUNNING", "COMPLETE", "FAILED", "CANCELLED"}

_ddb = None


def _get_ddb():
    """Return a lazily-initialised DynamoDB resource."""
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION)
    return _ddb


async def write_job_record(
    job_id: str,
    user_id: str,
    status: str,
    task_description: str = "",
    repo_url: str = "",
    base_branch: str = "",
    target_branch: str = "",
    runtime_session_id: str = "",
) -> None:
    """Write an initial job record for audit/history."""
    if status not in VALID_STATES:
        raise ValueError(f"Invalid status {status!r}; must be one of {VALID_STATES}")

    table = _get_ddb().Table(JOB_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(
        table.put_item,
        Item={
            "PK": f"user#{user_id}",
            "SK": f"job#{job_id}#{now}",
            "job_id": job_id,
            "user_id": user_id,
            "status": status,
            "task_description": task_description,
            "repo_url": repo_url,
            "base_branch": base_branch,
            "target_branch": target_branch,
            "runtime_session_id": runtime_session_id,
            "created_at": now,
            "GSI1PK": f"status#{status}",
            "GSI1SK": now,
        },
    )


async def update_job_status(
    job_id: str,
    user_id: str,
    status: str,
    **extra: Any,
) -> None:
    """Update a job record's status with optional extra attributes.

    Finds the record by querying PK=user#{user_id} and filtering on job_id,
    then applies an update_item call.

    Supported extra keys: pr_url, error, stop_reason,
    files_edited, duration_seconds, completed_at.
    """
    if status not in VALID_STATES:
        raise ValueError(f"Invalid status {status!r}; must be one of {VALID_STATES}")

    table = _get_ddb().Table(JOB_TABLE)

    # Find the SK for this job_id under the user's partition.
    resp = await asyncio.to_thread(
        table.query,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": f"user#{user_id}",
            ":sk_prefix": f"job#{job_id}#",
        },
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        raise LookupError(f"Job {job_id} not found for user {user_id}")

    sk = items[0]["SK"]

    # Build the update expression dynamically.
    update_parts = ["#st = :status", "#gsi1pk = :gsi1pk"]
    attr_names: dict[str, str] = {"#st": "status", "#gsi1pk": "GSI1PK"}
    attr_values: dict[str, Any] = {":status": status, ":gsi1pk": f"status#{status}"}

    allowed_extras = {
        "pr_url", "error", "stop_reason",
        "files_edited", "duration_seconds", "completed_at",
    }
    for key, value in extra.items():
        if key in allowed_extras and value is not None:
            # DynamoDB rejects Python floats — convert to Decimal.
            if isinstance(value, float):
                value = Decimal(str(value))
            placeholder = f":{key}"
            alias = f"#{key}"
            update_parts.append(f"{alias} = {placeholder}")
            attr_names[alias] = key
            attr_values[placeholder] = value

    await asyncio.to_thread(
        table.update_item,
        Key={"PK": f"user#{user_id}", "SK": sk},
        UpdateExpression="SET " + ", ".join(update_parts),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


async def query_job_record(job_id: str, user_id: str) -> Optional[dict]:
    """Query a single job record by job_id for a user.

    Returns the item dict or ``None`` if not found.
    """
    table = _get_ddb().Table(JOB_TABLE)
    resp = await asyncio.to_thread(
        table.query,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": f"user#{user_id}",
            ":sk_prefix": f"job#{job_id}#",
        },
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


async def query_user_jobs(
    user_id: str,
    status_filter: str = "",
    limit: int = 50,
) -> dict:
    """Query all jobs for a user with optional status filter.

    When *status_filter* is set, DynamoDB ``Limit`` caps items **scanned**
    (not items returned after filtering), so we paginate with
    ``LastEvaluatedKey`` until the requested number of filtered results is
    collected or no more items exist.  When no filter is set the existing
    single-query behaviour is preserved.

    Returns ``{"jobs": [...], "count": N}``.
    """
    table = _get_ddb().Table(JOB_TABLE)

    # Cap limit at 100 per requirement 6.4.
    effective_limit = min(limit, 100)

    base_kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
        "ExpressionAttributeValues": {
            ":pk": f"user#{user_id}",
            ":sk_prefix": "job#",
        },
        "ScanIndexForward": False,  # newest first
        "Limit": effective_limit,
    }

    if status_filter:
        if status_filter not in VALID_STATES:
            raise ValueError(
                f"Invalid status_filter {status_filter!r}; must be one of {VALID_STATES}"
            )
        base_kwargs["FilterExpression"] = "#st = :sf"
        base_kwargs.setdefault("ExpressionAttributeNames", {})["#st"] = "status"
        base_kwargs["ExpressionAttributeValues"][":sf"] = status_filter

        # Paginate until we have enough filtered results or exhaust all pages.
        jobs: list[dict] = []
        exclusive_start_key: dict | None = None
        while len(jobs) < effective_limit:
            kwargs = dict(base_kwargs)
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            resp = await asyncio.to_thread(table.query, **kwargs)
            jobs.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            exclusive_start_key = resp["LastEvaluatedKey"]
        jobs = jobs[:effective_limit]
        return {"jobs": jobs, "count": len(jobs)}

    # No filter -- single query is sufficient.
    resp = await asyncio.to_thread(table.query, **base_kwargs)
    jobs = resp.get("Items", [])
    return {"jobs": jobs, "count": len(jobs)}
