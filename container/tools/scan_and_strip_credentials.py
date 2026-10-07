# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Credential leak scanner — regex-based detection and stripping.

Scans modified files for credential patterns and replaces matches
with a redaction placeholder before push.

Requirements: 9.4, 21.1, 21.2, 21.3, 21.4, 21.5
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import TypedDict

from container.lib.git_safety import GIT_HARDEN_ARGS, hardened_git_env

logger = logging.getLogger(__name__)

# Hardening flags applied to every git invocation run against the
# post-agent repository. The pipeline restores ``.git/config`` before the
# scan, but ``.git/hooks`` and ``~/.gitconfig`` may still have been
# changed, and index-refreshing commands such as ``git diff`` can otherwise
# consult a planted fsmonitor. These are the same flags and env the push
# step uses (see ``container.lib.git_safety``).
_HARDEN = list(GIT_HARDEN_ARGS)

PATTERNS = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Temp Credentials", re.compile(r"ASIA[0-9A-Z]{16}")),
    ("API Key (sk-)", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,255}")),
    ("GitHub PAT (legacy)", re.compile(r"github_pat_[A-Za-z0-9_]{22,255}")),
    ("GitLab PAT", re.compile(r"glpat-[A-Za-z0-9\-_]{20,}")),
    ("PEM Private Key", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")),
    (
        "High-entropy assignment",
        re.compile(
            r'(?:secret|password|token|key)\s*[:=]\s*["\'][A-Za-z0-9+/=]{20,}["\']',
            re.IGNORECASE,
        ),
    ),
]

PLACEHOLDER = "<REDACTED_SECRET>"


class ScanResult(TypedDict):
    files_scanned: int
    files_modified: int
    findings: list[dict]


def _get_scan_files(work_dir: str, base_sha: str | None = None) -> list[str]:
    """Return file paths to scan, relative to *work_dir*, using git.

    Unions three sources so nothing an agent produced slips through:

    1. Commits the agent made itself: the diff ``base_sha..HEAD`` when a
       ``base_sha`` is supplied (names every file changed since the base
       branch tip, including files the agent committed). When ``base_sha``
       is falsy, this falls back to ``git diff --name-only HEAD`` to
       preserve the previous HEAD-based behaviour for standalone callers.
    2. Staged and unstaged working-tree edits (``git diff --name-only
       HEAD``) so uncommitted changes and the index are covered.
    3. Untracked files (``git ls-files --others --exclude-standard``).

    The diffs use ``--no-renames`` so the listing does not depend on rename
    detection heuristics: every added, modified or renamed-to path is named
    explicitly.
    """
    stdouts: list[str] = []
    env = hardened_git_env()

    if base_sha:
        committed = subprocess.run(
            ["git", *_HARDEN, "diff", "--name-only", "--no-renames",
             f"{base_sha}..HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        stdouts.append(committed.stdout)

    # Staged + unstaged working-tree changes (also covers the index).
    worktree = subprocess.run(
        ["git", *_HARDEN, "diff", "--name-only", "--no-renames", "HEAD"],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    stdouts.append(worktree.stdout)

    # Untracked files so nothing slips through.
    untracked = subprocess.run(
        ["git", *_HARDEN, "ls-files", "--others", "--exclude-standard"],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    stdouts.append(untracked.stdout)

    files: list[str] = []
    for stdout in stdouts:
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped:
                files.append(stripped)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


def scan_and_strip_content(content: str) -> tuple[str, list[dict]]:
    """Scan *content* for credential patterns, return (cleaned, findings)."""
    findings: list[dict] = []
    cleaned = content
    for pattern_name, regex in PATTERNS:
        for match in regex.finditer(cleaned):
            findings.append(
                {
                    "pattern": pattern_name,
                    "match": match.group()[:40],
                }
            )
        cleaned = regex.sub(PLACEHOLDER, cleaned)
    return cleaned, findings


def scan_and_strip_credentials(
    work_dir: str, job_id: str, base_sha: str | None = None
) -> ScanResult:
    """Scan modified files for credential leaks and strip secrets.

    Discovers changed files via the union of the ``base_sha..HEAD`` diff
    (so agent self-commits are included), the staged/unstaged working
    tree, and untracked files; checks each against the credential
    patterns, replaces matches with ``<REDACTED_SECRET>``, and writes
    back modified files.
    """
    modified_files = _get_scan_files(work_dir, base_sha)
    work_path = Path(work_dir)

    files_scanned = 0
    files_modified = 0
    all_findings: list[dict] = []

    for rel_path in modified_files:
        file_path = work_path / rel_path
        if not file_path.is_file():
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        files_scanned += 1
        cleaned, findings = scan_and_strip_content(content)

        if findings:
            file_findings = [
                {**f, "file": rel_path} for f in findings
            ]
            all_findings.extend(file_findings)
            file_path.write_text(cleaned, encoding="utf-8")
            files_modified += 1
            logger.warning(
                "[%s] Credentials detected in %s: %d finding(s)",
                job_id,
                rel_path,
                len(findings),
            )

    return ScanResult(
        files_scanned=files_scanned,
        files_modified=files_modified,
        findings=all_findings,
    )

