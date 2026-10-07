# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``serialize_job_record`` -- the single projection of a raw
DynamoDB job item shared by ``get_task_status`` and ``list_tasks``."""

from __future__ import annotations

import json
from decimal import Decimal

from container.lib.dynamodb_helpers import (
    JOB_PUBLIC_FIELDS,
    serialize_job_list,
    serialize_job_record,
)

_FULL_RAW_ITEM = {
    "PK": "user#alice",
    "SK": "job#abc#2025-01-01T00:00:00+00:00",
    "user_id": "alice",
    "runtime_session_id": "f67ddcd1-dc44-4867-8793-e4888e672b6b",
    "job_id": "abc",
    "status": "COMPLETE",
    "task_description": "do it",
    "repo_url": "https://github.com/o/r",
    "base_branch": "main",
    "target_branch": "opencode/abc",
    "pr_url": "https://github.com/o/r/pull/1",
    "stop_reason": "end_turn",
    "files_edited": ["README.md"],
    "duration_seconds": Decimal("17.41"),
    "error": "",
    "created_at": "2025-01-01T00:00:00+00:00",
    "completed_at": "2025-01-01T00:00:17+00:00",
}


class TestSerializeJobRecord:
    def test_exact_public_keys_in_order(self):
        out = serialize_job_record(_FULL_RAW_ITEM)
        assert tuple(out.keys()) == JOB_PUBLIC_FIELDS

    def test_internal_attributes_never_exposed(self):
        out = serialize_job_record(_FULL_RAW_ITEM)
        for key in ("PK", "SK", "user_id", "runtime_session_id"):
            assert key not in out

    def test_public_values_copied_through(self):
        out = serialize_job_record(_FULL_RAW_ITEM)
        for key in JOB_PUBLIC_FIELDS:
            if key == "duration_seconds":
                continue
            assert out[key] == _FULL_RAW_ITEM[key]

    def test_decimal_fraction_becomes_float(self):
        out = serialize_job_record({"duration_seconds": Decimal("17.41")})
        assert out["duration_seconds"] == 17.41
        assert isinstance(out["duration_seconds"], float)

    def test_decimal_integral_becomes_int(self):
        out = serialize_job_record({"duration_seconds": Decimal("5")})
        assert out["duration_seconds"] == 5
        assert isinstance(out["duration_seconds"], int)
        assert not isinstance(out["duration_seconds"], bool)

    def test_plain_numbers_untouched(self):
        assert serialize_job_record({"duration_seconds": 42})["duration_seconds"] == 42
        assert serialize_job_record({"duration_seconds": 1.5})["duration_seconds"] == 1.5

    def test_missing_fields_default(self):
        out = serialize_job_record({})
        assert tuple(out.keys()) == JOB_PUBLIC_FIELDS
        assert out["files_edited"] == []
        assert out["duration_seconds"] == 0
        for key in JOB_PUBLIC_FIELDS:
            if key in ("files_edited", "duration_seconds"):
                continue
            assert out[key] == ""

    def test_legacy_cancelled_row_without_duration_or_files(self):
        """Rows written before cancel_task populated these fields."""
        raw = {
            "PK": "user#a", "SK": "job#x#t", "job_id": "x", "user_id": "a",
            "status": "CANCELLED", "error": "Task cancelled by user",
            "created_at": "t", "completed_at": "t2",
        }
        out = serialize_job_record(raw)
        assert out["status"] == "CANCELLED"
        assert out["files_edited"] == []
        assert out["duration_seconds"] == 0
        assert out["pr_url"] == ""

    def test_unknown_keys_dropped(self):
        out = serialize_job_record({**_FULL_RAW_ITEM, "mystery": 1, "another": "x"})
        assert "mystery" not in out
        assert "another" not in out
        assert set(out.keys()) == set(JOB_PUBLIC_FIELDS)

    def test_result_is_json_serialisable(self):
        out = serialize_job_record(_FULL_RAW_ITEM)
        dumped = json.loads(json.dumps(out))
        assert dumped["duration_seconds"] == 17.41

    def test_files_edited_copied_not_aliased(self):
        files = ["a.py"]
        out = serialize_job_record({"files_edited": files})
        assert out["files_edited"] == files
        assert out["files_edited"] is not files

    def test_input_not_mutated(self):
        raw = dict(_FULL_RAW_ITEM)
        serialize_job_record(raw)
        assert raw == _FULL_RAW_ITEM


class TestSerializeJobList:
    def test_applies_to_every_item(self):
        items = [_FULL_RAW_ITEM, {"job_id": "b", "status": "RUNNING"}]
        out = serialize_job_list(items)
        assert len(out) == 2
        for rec in out:
            assert tuple(rec.keys()) == JOB_PUBLIC_FIELDS
            assert "PK" not in rec

    def test_empty(self):
        assert serialize_job_list([]) == []
