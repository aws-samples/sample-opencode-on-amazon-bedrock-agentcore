#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# cleanup-retained-resources.sh — Remove resources left behind after `cdk destroy`.
#
# The DynamoDB job table uses a RETAIN removal policy to prevent accidental
# data loss. After `cdk destroy` it remains and will cause an "already exists"
# error on the next `cdk deploy`. This script removes it. (CloudWatch log
# groups are also retained but have CDK-generated names and do not collide
# on redeploy.)
#
# Also cleans up security groups and subnets that fail to delete during
# `cdk destroy` because AgentCore-managed ENIs haven't been released yet.
#
# Usage:
#   export AWS_PROFILE=my-profile   # optional
#   export AWS_REGION=us-east-1
#   ./scripts/cleanup-retained-resources.sh
#
# Prerequisites: AWS CLI v2, jq

set -euo pipefail

REGION="${AWS_REGION:?Set AWS_REGION before running this script}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")

echo "=== Cleaning up retained OpenCode resources in $REGION ($ACCOUNT) ==="
echo ""

# Cedar policies created by create-policies.py block deletion of the policy
# engine in the OpenCodeGateway stack. Point at the flag that removes them.
GW_STATUS=$(aws cloudformation describe-stacks --stack-name OpenCodeGateway --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)
if [ "$GW_STATUS" = "DELETE_FAILED" ]; then
    echo "  NOTE: OpenCodeGateway is DELETE_FAILED. If the reason is 'Policy engine still contains"
    echo "        N policies', run: python scripts/create-policies.py --delete --region $REGION"
    echo "        then re-run 'cdk destroy'."
    echo ""
fi

# -----------------------------------------------------------------------
# 1. DynamoDB table
# -----------------------------------------------------------------------
echo "--- DynamoDB ---"
if aws dynamodb describe-table --table-name opencode-jobs --region "$REGION" &>/dev/null; then
    echo "  Deleting table: opencode-jobs"
    aws dynamodb delete-table --table-name opencode-jobs --region "$REGION" --output text --query 'TableDescription.TableStatus'
else
    echo "  Table opencode-jobs not found (OK)"
fi

# -----------------------------------------------------------------------
# 2. Security groups (AgentCore ENIs may hold these after destroy)
# -----------------------------------------------------------------------
echo ""
echo "--- Security Groups (OpenCode tagged) ---"
SG_IDS=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters Name=tag:Project,Values=OpenCode \
    --query 'SecurityGroups[*].GroupId' --output text 2>/dev/null || true)
if [ -n "$SG_IDS" ]; then
    for SG in $SG_IDS; do
        echo "  Deleting security group: $SG"
        # Detach any ENIs first
        ENI_IDS=$(aws ec2 describe-network-interfaces --region "$REGION" \
            --filters Name=group-id,Values="$SG" \
            --query 'NetworkInterfaces[*].NetworkInterfaceId' --output text 2>/dev/null || true)
        for ENI in $ENI_IDS; do
            ATTACH=$(aws ec2 describe-network-interfaces --region "$REGION" \
                --network-interface-ids "$ENI" \
                --query 'NetworkInterfaces[0].Attachment.AttachmentId' --output text 2>/dev/null || true)
            if [ -n "$ATTACH" ] && [ "$ATTACH" != "None" ]; then
                echo "    Detaching ENI $ENI (attachment $ATTACH)"
                aws ec2 detach-network-interface --attachment-id "$ATTACH" --region "$REGION" --force 2>/dev/null || true
                sleep 5
            fi
            echo "    Deleting ENI $ENI"
            aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$REGION" 2>/dev/null || true
        done
        aws ec2 delete-security-group --group-id "$SG" --region "$REGION" 2>/dev/null \
            && echo "    Deleted $SG" \
            || echo "    Could not delete $SG (ENIs may still be releasing — retry in a few minutes)"
    done
else
    echo "  No OpenCode security groups found (OK)"
fi

# -----------------------------------------------------------------------
# 3. Orphaned VPCs (retained subnets prevent VPC deletion during destroy)
# -----------------------------------------------------------------------
echo ""
echo "--- VPCs (OpenCode tagged) ---"
VPC_IDS=$(aws ec2 describe-vpcs --region "$REGION" \
    --filters Name=tag:Project,Values=OpenCode \
    --query 'Vpcs[*].VpcId' --output text 2>/dev/null || true)
if [ -n "$VPC_IDS" ]; then
    for VPC in $VPC_IDS; do
        echo "  Cleaning up VPC: $VPC"
        # Delete subnets
        SUBNET_IDS=$(aws ec2 describe-subnets --region "$REGION" \
            --filters Name=vpc-id,Values="$VPC" \
            --query 'Subnets[*].SubnetId' --output text 2>/dev/null || true)
        for SUBNET in $SUBNET_IDS; do
            echo "    Deleting subnet $SUBNET"
            aws ec2 delete-subnet --subnet-id "$SUBNET" --region "$REGION" 2>/dev/null || true
        done
        # Delete the VPC
        aws ec2 delete-vpc --vpc-id "$VPC" --region "$REGION" 2>/dev/null \
            && echo "    Deleted VPC $VPC" \
            || echo "    Could not delete VPC $VPC (may have remaining dependencies)"
    done
else
    echo "  No OpenCode VPCs found (OK)"
fi

echo ""
echo "=== Cleanup complete ==="
