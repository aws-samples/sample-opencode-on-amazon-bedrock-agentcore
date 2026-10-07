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

# OpenCode — download the pinned GitHub release asset directly from
# anomalyco/opencode (formerly sst/opencode) and verify a pinned SHA-256
# before installing. No `curl | bash`. GitHub issues #7960 and #7843
# report bunfs incompatibilities with PRoot; AgentCore uses microVMs
# (not PRoot), so the plain release binary works.
#
# The stack builds this image for linux/arm64 only (Platform.LINUX_ARM64 in
# stacks/agentcore_stack.py), so only the arm64 asset is pinned. Bumping
# OPENCODE_VERSION requires updating the checksum below; the build fails
# on mismatch. The checksum is the SHA-256 of the release tarball (it
# matches the `digest` field in the GitHub releases API).
# tar and gzip are Debian Essential packages, so the slim base has them.
ARG OPENCODE_VERSION=1.18.34
ARG OPENCODE_SHA256_ARM64=bbdb3f00c2c51e42e315525233151309724226a8776da8e9145e3b0fa3d5310f
RUN set -eu; \
    asset="opencode-linux-arm64.tar.gz"; \
    tmp="$(mktemp -d)"; \
    curl -fsSL -o "${tmp}/${asset}" \
      "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/${asset}"; \
    echo "${OPENCODE_SHA256_ARM64}  ${tmp}/${asset}" | sha256sum -c -; \
    tar -xzf "${tmp}/${asset}" -C "${tmp}" opencode; \
    install -m 0755 "${tmp}/opencode" /usr/local/bin/opencode; \
    chmod a+rx /usr/local/bin/opencode; \
    rm -rf "${tmp}"
ENV OPENCODE_BINARY="/usr/local/bin/opencode"

# Copy into container/ subdirectory so "from container.tools import ..." resolves
COPY . ./container/

RUN useradd -m -s /bin/bash opencode
RUN chown -R opencode:opencode /app

USER opencode

EXPOSE 8000

ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "container.code_mcp_server"]
