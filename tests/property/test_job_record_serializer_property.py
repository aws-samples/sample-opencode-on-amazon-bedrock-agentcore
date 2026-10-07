# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: ``serialize_job_record`` always yields exactly
``JOB_PUBLIC_FIELDS`` and a JSON-serialisable dict (no Decimal leaks, no
internal attributes), for any raw DynamoDB item.

Property: for any raw item built from the job-record strategies plus random
extra keys and optional internal attributes, ``serialize_job_record(item)``
has exactly ``JOB_PUBLIC_FIELDS`` as keys, ``json.dumps`` succeeds, and
``duration_seconds`` is an int or float.
"""

from __future__ import annotations

import json
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from container.lib.dynamodb_helpers import (
    JOB_PUBLIC_FIELDS,
    VALID_STATES,
    serialize_job_record,
)

_text = st.text(min_size=0, max_size=100)
_decimal = st.one_of(
    st.integers(min_value=0, max_value=86400).map(lambda n: Decimal(n)),
    st.floats(min_value=0, max_value=86400, allow_nan=False, allow_infinity=False)
    .map(lambda f: Decimal(str(f))),
)
_duration = st.one_of(
    st.integers(min_value=0, max_value=86400),
    st.floats(min_value=0, max_value=86400, allow_nan=False, allow_infinity=False),
    _decimal,
)

# Every public field is optional so the strategy also produces legacy rows.
_public_fields = st.fixed_dictionaries(
    {},
    optional={
        "job_id": st.uuids().map(str),
        "status": st.sampled_from(sorted(VALID_STATES)),
        "task_description": _text,
        "repo_url": _text,
        "base_branch": _text,
        "target_branch": _text,
        "pr_url": _text,
        "stop_reason": _text,
        "files_edited": st.lists(st.text(min_size=1, max_size=50), max_size=10),
        "duration_seconds": _duration,
        "error": _text,
        "created_at": _text,
        "completed_at": _text,
    },
)

_internal_fields = st.fixed_dictionaries(
    {},
    optional={
        "PK": _text.map(lambda s: f"user#{s}"),
        "SK": _text.map(lambda s: f"job#{s}"),
        "user_id": _text,
        "runtime_session_id": _text,
    },
)

_extra_key = st.text(min_size=1, max_size=20).filter(
    lambda k: k not in JOB_PUBLIC_FIELDS
    and k not in ("PK", "SK", "user_id", "runtime_session_id")
)
_extra_fields = st.dictionaries(
    _extra_key,
    st.one_of(_text, st.integers(), _decimal, st.lists(_text, max_size=3)),
    max_size=5,
)

_raw_item = st.builds(
    lambda pub, internal, extra: {**extra, **internal, **pub},
    _public_fields, _internal_fields, _extra_fields,
)


class TestSerializeJobRecordProperty:
    @given(item=_raw_item)
    @settings(max_examples=200)
    def test_exact_keys_and_json_serialisable(self, item):
        out = serialize_job_record(item)

        assert tuple(out.keys()) == JOB_PUBLIC_FIELDS
        for key in ("PK", "SK", "user_id", "runtime_session_id"):
            assert key not in out

        # No Decimal may leak -- json.dumps must succeed as-is.
        json.dumps(out)
        assert isinstance(out["duration_seconds"], (int, float))
        assert isinstance(out["files_edited"], list)

    @given(item=_raw_item)
    @settings(max_examples=200)
    def test_values_preserved_or_defaulted(self, item):
        out = serialize_job_record(item)
        for key in JOB_PUBLIC_FIELDS:
            if key not in item:
                expected = [] if key == "files_edited" else 0 if key == "duration_seconds" else ""
                assert out[key] == expected
            elif key == "duration_seconds":
                # Decimal -> float conversion must be value-preserving to
                # float precision.
                assert out[key] == float(item[key])
            else:
                assert out[key] == item[key]
