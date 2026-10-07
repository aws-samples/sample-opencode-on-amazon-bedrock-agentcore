# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for Job Store stack (stacks/job_store_stack.py).

Validates: Requirements 7.1, 12.1
- Jobs table key schema matches design (PK, SK), no GSI
- KMS encryption is configured
- Point-in-time recovery is enabled
"""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import assertions
import pytest

from stacks.security_stack import SecurityStack
from stacks.job_store_stack import JobStoreStack

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CDK_JSON_PATH = Path(__file__).resolve().parents[2] / "cdk.json"


def _load_cdk_context() -> dict:
    with open(CDK_JSON_PATH) as f:
        return json.load(f)["context"]


def _build_job_store_template() -> assertions.Template:
    ctx = _load_cdk_context()
    app = cdk.App(context=ctx)
    env = cdk.Environment(account="123456789012", region="us-east-1")
    security_stack = SecurityStack(app, "TestSecurity", env=env)
    stack = JobStoreStack(app, "TestJobStore", cmk=security_stack.cmk, env=env)
    return assertions.Template.from_stack(stack)


def _get_tables(tpl: dict) -> dict[str, dict]:
    """Return a mapping of table_name → resource properties for DynamoDB tables."""
    tables = {}
    for lid, res in tpl["Resources"].items():
        if res["Type"] == "AWS::DynamoDB::Table":
            name = res["Properties"].get("TableName", lid)
            tables[name] = res["Properties"]
    return tables


# ---------------------------------------------------------------------------
# Jobs table — key schema tests (Requirement 7.1)
# ---------------------------------------------------------------------------

class TestJobsTableKeySchema:
    """Verify opencode-jobs table PK, SK, and GSI key schemas."""

    def test_jobs_table_exists(self):
        template = _build_job_store_template()
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"TableName": "opencode-jobs"},
        )

    def test_jobs_table_pk_is_string(self):
        """PK attribute named 'PK' with type String."""
        template = _build_job_store_template()
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "opencode-jobs",
                "KeySchema": assertions.Match.array_with([
                    assertions.Match.object_like({"AttributeName": "PK", "KeyType": "HASH"}),
                ]),
                "AttributeDefinitions": assertions.Match.array_with([
                    assertions.Match.object_like({"AttributeName": "PK", "AttributeType": "S"}),
                ]),
            },
        )

    def test_jobs_table_sk_is_string(self):
        """SK attribute named 'SK' with type String."""
        template = _build_job_store_template()
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "opencode-jobs",
                "KeySchema": assertions.Match.array_with([
                    assertions.Match.object_like({"AttributeName": "SK", "KeyType": "RANGE"}),
                ]),
                "AttributeDefinitions": assertions.Match.array_with([
                    assertions.Match.object_like({"AttributeName": "SK", "AttributeType": "S"}),
                ]),
            },
        )

    def test_jobs_table_has_no_gsi(self):
        """Queries are user-partitioned on the base table; no GSI exists."""
        template = _build_job_store_template()
        tpl = template.to_json()
        table = next(
            r for r in tpl["Resources"].values()
            if r["Type"] == "AWS::DynamoDB::Table"
        )
        assert "GlobalSecondaryIndexes" not in table["Properties"]



# ---------------------------------------------------------------------------
# KMS encryption tests (Requirement 10.4, 12.1)
# ---------------------------------------------------------------------------

class TestKmsEncryption:
    """Verify jobs table uses customer-managed KMS encryption."""

    def test_jobs_table_uses_kms_encryption(self):
        template = _build_job_store_template()
        tpl = template.to_json()
        tables = _get_tables(tpl)
        sse = tables["opencode-jobs"].get("SSESpecification", {})
        assert sse.get("SSEEnabled") is True, "Jobs table SSE not enabled"
        assert sse.get("SSEType") == "KMS", "Jobs table not using KMS encryption"
        assert sse.get("KMSMasterKeyId") is not None, "Jobs table missing KMS key reference"


# ---------------------------------------------------------------------------
# Point-in-time recovery tests (Requirement 12.1)
# ---------------------------------------------------------------------------

class TestPointInTimeRecovery:
    """Verify PITR is enabled on jobs table."""

    def test_jobs_table_pitr_enabled(self):
        template = _build_job_store_template()
        tpl = template.to_json()
        tables = _get_tables(tpl)
        pitr = tables["opencode-jobs"].get("PointInTimeRecoverySpecification", {})
        assert pitr.get("PointInTimeRecoveryEnabled") is True, (
            "Jobs table PITR not enabled"
        )


# ---------------------------------------------------------------------------
# Table count and billing mode
# ---------------------------------------------------------------------------

class TestTableBasics:
    """Verify table count and billing mode."""

    def test_one_dynamodb_table_created(self):
        """Only 1 DynamoDB table (opencode-jobs)."""
        template = _build_job_store_template()
        template.resource_count_is("AWS::DynamoDB::Table", 1)

    def test_jobs_table_pay_per_request(self):
        template = _build_job_store_template()
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "opencode-jobs",
                "BillingMode": "PAY_PER_REQUEST",
            },
        )

    def test_jobs_table_retained_on_delete(self):
        template = _build_job_store_template()
        tpl = template.to_json()
        for lid, res in tpl["Resources"].items():
            if res["Type"] == "AWS::DynamoDB::Table" and res["Properties"].get("TableName") == "opencode-jobs":
                assert res.get("DeletionPolicy") == "Retain" or res.get("UpdateReplacePolicy") == "Retain", (
                    "Jobs table should have Retain removal policy"
                )
                break


# ---------------------------------------------------------------------------
# No alarms / topics
# ---------------------------------------------------------------------------

class TestNoSnsOrAlarms:
    def test_no_sns_or_alarms(self):
        template = _build_job_store_template()
        template.resource_count_is("AWS::SNS::Topic", 0)
        template.resource_count_is("AWS::CloudWatch::Alarm", 0)
