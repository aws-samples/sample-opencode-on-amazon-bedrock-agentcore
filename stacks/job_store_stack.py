# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OpenCode Job Store stack — DynamoDB table for job audit/history (user-scoped).

Simplified 4-state model: RUNNING, COMPLETE, FAILED, CANCELLED.
DynamoDB is used for lightweight audit and history records only — not as a state machine.

PK: user#{user_id}   SK: job#{job_id}#{created_at_iso}

Record attributes:
  job_id, user_id, status, task_description, repo_url, base_branch,
  target_branch, runtime_session_id, pr_url, stop_reason,
  files_edited, duration_seconds, error, created_at, completed_at

Requirements: 8.1, 8.5
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    RemovalPolicy,
)
import cdk_nag
from constructs import Construct


class JobStoreStack(cdk.Stack):
    """DynamoDB Job Store table (user-partitioned, 4-state audit/history)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -----------------------------------------------------------------
        # Jobs table (opencode-jobs)
        # PK: user#{user_id}  SK: job#{job_id}#{created_at_iso}
        # States: RUNNING | COMPLETE | FAILED | CANCELLED
        # -----------------------------------------------------------------
        self.job_table = dynamodb.Table(
            self,
            "JobsTable",
            table_name="opencode-jobs",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cmk,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -----------------------------------------------------------------
        # cdk-nag suppressions
        # -----------------------------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.job_table,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-DDB3",
                    reason="Point-in-time recovery is enabled via point_in_time_recovery=True.",
                ),
            ],
        )
