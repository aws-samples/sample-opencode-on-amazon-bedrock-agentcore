# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: DynamoDB job record round-trip and update extras.

**Validates: Requirements 3.3, 3.4, 5.1, 5.2**

Property 4 -- DynamoDB job record write/query round-trip:
  For any valid job record inputs, writing via write_job_record then
  querying via query_job_record SHALL return a record with matching key fields.

Property 5 -- DynamoDB update extras persistence:
  For any subset of allowed extras with non-None values, calling
  update_job_status SHALL include all provided extras in the DynamoDB
  update expression.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from botocore.exceptions import ClientError

from container.lib.dynamodb_helpers import (
    JobStateConflict,
    write_job_record,
    update_job_status,
    query_job_record,
    VALID_STATES,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Identifiers: non-empty alphanumeric strings (safe for DynamoDB keys)
_alnum = st.sampled_from(
    list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
)

_job_id = st.text(alphabet=_alnum, min_size=4, max_size=36)
_user_id = st.text(alphabet=_alnum, min_size=1, max_size=40)
_status = st.sampled_from(sorted(VALID_STATES))
_text_field = st.text(min_size=0, max_size=100)
_url = st.from_regex(r"https://[a-z]{3,10}\.[a-z]{2,5}/[a-z]{1,20}", fullmatch=True)
_branch = st.from_regex(r"[a-zA-Z][a-zA-Z0-9\-_]{0,20}", fullmatch=True)

# Allowed extras for update_job_status
_pr_url = st.one_of(st.none(), _url)
_error_msg = st.one_of(st.none(), st.text(min_size=1, max_size=200))
_stop_reason = st.one_of(st.none(), st.sampled_from(["end_turn", "max_tokens", "tool_use", "error"]))
_files_edited = st.one_of(st.none(), st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=5))
_duration_seconds = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=86400),
    st.floats(min_value=0, max_value=86400, allow_nan=False, allow_infinity=False),
)
_completed_at = st.one_of(st.none(), st.from_regex(r"2024-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", fullmatch=True))


# ---------------------------------------------------------------------------
# Property 4: DynamoDB job record write/query round-trip
# ---------------------------------------------------------------------------


class TestDynamoDBRoundTrip:
    """**Validates: Requirements 3.3**"""

    @given(
        job_id=_job_id,
        user_id=_user_id,
        status=_status,
        task_description=_text_field,
        repo_url=_url,
        base_branch=_branch,
        target_branch=_branch,
    )
    @settings(max_examples=100, deadline=10_000)
    @pytest.mark.asyncio
    async def test_write_then_query_returns_matching_record(
        self, job_id, user_id, status, task_description, repo_url,
        base_branch, target_branch,
    ):
        """For any valid job record inputs, write then query SHALL return
        matching record."""
        # Storage for items written via put_item
        stored_items: list[dict] = []

        mock_table = MagicMock()
        mock_table.put_item = lambda **kwargs: stored_items.append(kwargs["Item"])

        def mock_query(**kwargs):
            """Simulate DynamoDB query by filtering stored items."""
            expr_values = kwargs.get("ExpressionAttributeValues", {})
            pk = expr_values.get(":pk", "")
            sk_prefix = expr_values.get(":sk_prefix", "")
            matching = [
                item for item in stored_items
                if item["PK"] == pk and item["SK"].startswith(sk_prefix)
            ]
            return {"Items": matching[:1]}

        mock_table.query = lambda **kwargs: mock_query(**kwargs)

        with patch("container.lib.dynamodb_helpers._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table

            # Write
            await write_job_record(
                job_id=job_id,
                user_id=user_id,
                status=status,
                task_description=task_description,
                repo_url=repo_url,
                base_branch=base_branch,
                target_branch=target_branch,
            )

            # Query
            record = await query_job_record(job_id=job_id, user_id=user_id)

        assert record is not None, "query_job_record returned None after write"
        assert record["job_id"] == job_id
        assert record["user_id"] == user_id
        assert record["status"] == status
        assert record["task_description"] == task_description
        assert record["repo_url"] == repo_url
        assert record["base_branch"] == base_branch
        assert record["target_branch"] == target_branch
        assert record["PK"] == f"user#{user_id}"
        assert record["SK"].startswith(f"job#{job_id}#")


# ---------------------------------------------------------------------------
# Property 5: DynamoDB update extras persistence
# ---------------------------------------------------------------------------


class TestDynamoDBUpdateExtras:
    """**Validates: Requirements 3.4**"""

    @given(
        job_id=_job_id,
        user_id=_user_id,
        initial_status=st.just("RUNNING"),
        new_status=st.sampled_from(["COMPLETE", "FAILED", "CANCELLED"]),
        pr_url=_pr_url,
        error=_error_msg,
        stop_reason=_stop_reason,
        files_edited=_files_edited,
        duration_seconds=_duration_seconds,
        completed_at=_completed_at,
    )
    @settings(max_examples=100, deadline=10_000)
    @pytest.mark.asyncio
    async def test_update_includes_all_provided_extras(
        self, job_id, user_id, initial_status, new_status,
        pr_url, error, stop_reason, files_edited, duration_seconds, completed_at,
    ):
        """For any subset of allowed extras, update_job_status SHALL include
        all in update expression."""
        # Build the extras dict (only non-None values)
        extras = {}
        if pr_url is not None:
            extras["pr_url"] = pr_url
        if error is not None:
            extras["error"] = error
        if stop_reason is not None:
            extras["stop_reason"] = stop_reason
        if files_edited is not None:
            extras["files_edited"] = files_edited
        if duration_seconds is not None:
            extras["duration_seconds"] = duration_seconds
        if completed_at is not None:
            extras["completed_at"] = completed_at

        # At least one extra should be provided for a meaningful test
        assume(len(extras) > 0)

        # Simulate an existing record
        existing_sk = f"job#{job_id}#2024-01-01T00:00:00+00:00"
        existing_item = {
            "PK": f"user#{user_id}",
            "SK": existing_sk,
            "job_id": job_id,
            "user_id": user_id,
            "status": initial_status,
        }

        captured_updates: list[dict] = []

        mock_table = MagicMock()

        def mock_query(**kwargs):
            return {"Items": [existing_item]}

        def mock_update_item(**kwargs):
            captured_updates.append(kwargs)

        mock_table.query = lambda **kwargs: mock_query(**kwargs)
        mock_table.update_item = lambda **kwargs: mock_update_item(**kwargs)

        with patch("container.lib.dynamodb_helpers._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table

            await update_job_status(
                job_id=job_id,
                user_id=user_id,
                status=new_status,
                **extras,
            )

        assert len(captured_updates) == 1, "Expected exactly one update_item call"
        update_call = captured_updates[0]

        update_expr = update_call["UpdateExpression"]
        attr_names = update_call["ExpressionAttributeNames"]
        attr_values = update_call["ExpressionAttributeValues"]

        # Status should always be in the update
        assert ":status" in attr_values
        assert attr_values[":status"] == new_status

        # Terminal writes are conditional on the row still being RUNNING
        # by default, so a late COMPLETE can never overwrite CANCELLED.
        assert update_call["ConditionExpression"] == "#st = :expected"
        assert attr_values[":expected"] == "RUNNING"
        assert attr_names["#st"] == "status"

        # Every provided extra should appear in the update expression
        for key, value in extras.items():
            placeholder = f":{key}"
            alias = f"#{key}"
            assert placeholder in attr_values, (
                f"Extra '{key}' value not in ExpressionAttributeValues"
            )
            # Floats are converted to Decimal for DynamoDB compatibility.
            expected = Decimal(str(value)) if isinstance(value, float) else value
            assert attr_values[placeholder] == expected, (
                f"Extra '{key}' value mismatch: expected {expected!r}, "
                f"got {attr_values[placeholder]!r}"
            )
            assert alias in attr_names, (
                f"Extra '{key}' alias not in ExpressionAttributeNames"
            )
            assert attr_names[alias] == key
            assert alias in update_expr, (
                f"Extra '{key}' alias not in UpdateExpression"
            )


# ---------------------------------------------------------------------------
# Conditional terminal write (expected_status / JobStateConflict)
# ---------------------------------------------------------------------------


def _mock_table_with_record(job_id: str, user_id: str, status: str = "RUNNING"):
    existing_item = {
        "PK": f"user#{user_id}",
        "SK": f"job#{job_id}#2024-01-01T00:00:00+00:00",
        "job_id": job_id,
        "user_id": user_id,
        "status": status,
    }
    captured: list[dict] = []
    mock_table = MagicMock()
    mock_table.query = lambda **kwargs: {"Items": [existing_item]}
    mock_table.update_item = lambda **kwargs: captured.append(kwargs)
    return mock_table, captured


class TestDynamoDBConditionalUpdate:
    """update_job_status is conditional by default and raises JobStateConflict
    when DynamoDB reports ConditionalCheckFailedException."""

    @given(
        job_id=_job_id,
        user_id=_user_id,
        new_status=st.sampled_from(["COMPLETE", "FAILED", "CANCELLED"]),
        expected=st.sampled_from(sorted(VALID_STATES)),
    )
    @settings(max_examples=50, deadline=10_000)
    @pytest.mark.asyncio
    async def test_explicit_expected_status_is_forwarded(
        self, job_id, user_id, new_status, expected
    ):
        """Any explicit expected_status lands in ':expected'."""
        mock_table, captured = _mock_table_with_record(job_id, user_id)
        with patch("container.lib.dynamodb_helpers._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            await update_job_status(
                job_id=job_id, user_id=user_id, status=new_status,
                expected_status=expected, completed_at="2024-01-01T00:00:01",
            )
        assert len(captured) == 1
        assert captured[0]["ConditionExpression"] == "#st = :expected"
        assert captured[0]["ExpressionAttributeValues"][":expected"] == expected

    @given(
        job_id=_job_id,
        user_id=_user_id,
        new_status=st.sampled_from(["COMPLETE", "FAILED", "CANCELLED"]),
    )
    @settings(max_examples=50, deadline=10_000)
    @pytest.mark.asyncio
    async def test_expected_status_none_is_unconditional(
        self, job_id, user_id, new_status
    ):
        """expected_status=None omits ConditionExpression and ':expected'."""
        mock_table, captured = _mock_table_with_record(job_id, user_id)
        with patch("container.lib.dynamodb_helpers._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            await update_job_status(
                job_id=job_id, user_id=user_id, status=new_status,
                expected_status=None, completed_at="2024-01-01T00:00:01",
            )
        assert len(captured) == 1
        assert "ConditionExpression" not in captured[0]
        assert ":expected" not in captured[0]["ExpressionAttributeValues"]

    @given(
        job_id=_job_id,
        user_id=_user_id,
        new_status=st.sampled_from(["COMPLETE", "FAILED", "CANCELLED"]),
    )
    @settings(max_examples=50, deadline=10_000)
    @pytest.mark.asyncio
    async def test_conditional_check_failure_raises_job_state_conflict(
        self, job_id, user_id, new_status
    ):
        """ConditionalCheckFailedException -> JobStateConflict (chained)."""
        mock_table, _captured = _mock_table_with_record(job_id, user_id, "CANCELLED")

        def _refuse(**kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "The conditional request failed",
                    }
                },
                "UpdateItem",
            )

        mock_table.update_item = _refuse
        with patch("container.lib.dynamodb_helpers._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            with pytest.raises(JobStateConflict) as excinfo:
                await update_job_status(
                    job_id=job_id, user_id=user_id, status=new_status,
                    completed_at="2024-01-01T00:00:01",
                )
        assert job_id in str(excinfo.value)
        assert "RUNNING" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ClientError)

    @pytest.mark.asyncio
    async def test_other_client_errors_propagate_unchanged(self):
        """Non-conditional ClientErrors are re-raised as-is."""
        mock_table, _captured = _mock_table_with_record("job1", "user1")

        def _throttle(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "slow down"}},
                "UpdateItem",
            )

        mock_table.update_item = _throttle
        with patch("container.lib.dynamodb_helpers._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            with pytest.raises(ClientError) as excinfo:
                await update_job_status(
                    job_id="job1", user_id="user1", status="COMPLETE",
                )
        assert excinfo.value.response["Error"]["Code"] == (
            "ProvisionedThroughputExceededException"
        )
