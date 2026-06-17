# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# OpenCode on AgentCore — Container Image
# Single-stage build, ARM64, Python 3.12
# Single FastMCP server on port 8000
# Config: autoupdate=false, disabled_providers=[opencode]
# Model: ARN-based inference profile for cross-region
# Requirements: 1.3, 1.4, 11.1, 11.2, 11.3, 11.4, 11.5

FROM public.ecr.aws/docker/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN opentelemetry-bootstrap -a install

# OpenCode — install via the official curl installer. GitHub issues
# #7960 and #7843 report bunfs incompatibilities with PRoot; AgentCore
# uses microVMs (not PRoot), so the simple installer path is worth
# trying. Install as root then copy to /usr/local/bin so the 'opencode'
# user can run the binary.
ARG OPENCODE_VERSION=1.14.19
# The installer needs $SHELL set (it modifies shell config), and expects
# $VERSION (not $OPENCODE_VERSION) for pinning.
RUN SHELL=/bin/bash VERSION=${OPENCODE_VERSION} bash -c \
    "curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path" && \
    cp /root/.opencode/bin/opencode /usr/local/bin/opencode && \
    chmod a+rx /usr/local/bin/opencode
ENV OPENCODE_BINARY="/usr/local/bin/opencode"

# Copy into container/ subdirectory so "from container.tools import ..." resolves
COPY . ./container/

RUN useradd -m -s /bin/bash opencode
RUN chown -R opencode:opencode /app

USER opencode

EXPOSE 8000

ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "container.code_mcp_server"]
