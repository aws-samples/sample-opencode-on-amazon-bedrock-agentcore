# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for scan_and_strip_credentials tool.

Requirements: 9.4, 21.1, 21.2, 21.3, 21.4, 21.5
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from container.tools.scan_and_strip_credentials import (
    PATTERNS,
    PLACEHOLDER,
    ScanResult,
    _get_scan_files,
    scan_and_strip_content,
    scan_and_strip_credentials,
)


# ---------------------------------------------------------------------------
# scan_and_strip_content — pure function tests
# ---------------------------------------------------------------------------


class TestScanAndStripContent:
    """Tests for the pure scan_and_strip_content helper."""

    def test_detects_aws_access_key(self):
        content = "aws_key = AKIAIOSFODNN7EXAMPLE"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
        assert len(findings) == 1
        assert findings[0]["pattern"] == "AWS Access Key"

    def test_detects_sk_api_key(self):
        content = 'api_key = "sk-abcdefghijklmnopqrstuvwx"'
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert "sk-abcdefghijklmnopqrstuvwx" not in cleaned
        assert any(f["pattern"] == "API Key (sk-)" for f in findings)

    def test_detects_pem_private_key(self):
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert "-----BEGIN RSA PRIVATE KEY-----" not in cleaned
        assert any(f["pattern"] == "PEM Private Key" for f in findings)

    def test_detects_generic_private_key(self):
        content = "-----BEGIN PRIVATE KEY-----"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert any(f["pattern"] == "PEM Private Key" for f in findings)

    def test_detects_high_entropy_assignment(self):
        secret_value = "A" * 25
        content = f'secret = "{secret_value}"'
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert any(f["pattern"] == "High-entropy assignment" for f in findings)

    def test_high_entropy_case_insensitive(self):
        secret_value = "B" * 25
        content = f"SECRET = '{secret_value}'"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert len(findings) >= 1

    def test_no_credentials_returns_unchanged(self):
        content = "print('hello world')\nx = 42\n"
        cleaned, findings = scan_and_strip_content(content)
        assert cleaned == content
        assert findings == []

    def test_multiple_patterns_in_same_content(self):
        content = (
            "key1 = AKIAIOSFODNN7EXAMPLE\n"
            "key2 = sk-abcdefghijklmnopqrstuvwx\n"
            "-----BEGIN PRIVATE KEY-----\n"
        )
        cleaned, findings = scan_and_strip_content(content)
        assert cleaned.count(PLACEHOLDER) == 3
        pattern_names = {f["pattern"] for f in findings}
        assert "AWS Access Key" in pattern_names
        assert "API Key (sk-)" in pattern_names
        assert "PEM Private Key" in pattern_names

    # --- New patterns (Req 12) ---

    def test_detects_aws_temp_credentials(self):
        content = "temp_key = ASIAJEXAMPLEKEYID1234"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert "ASIAJEXAMPLEKEYID1234" not in cleaned
        assert any(f["pattern"] == "AWS Temp Credentials" for f in findings)

    def test_detects_github_fine_grained_token_ghp(self):
        token = "ghp_" + "A" * 36
        content = f"GITHUB_TOKEN={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitHub Token" for f in findings)

    def test_detects_github_token_gho(self):
        token = "gho_" + "B" * 36
        content = f"token = {token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitHub Token" for f in findings)

    def test_detects_github_token_ghs(self):
        token = "ghs_" + "C" * 40
        content = f"GH_TOKEN={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitHub Token" for f in findings)

    def test_detects_github_token_ghu(self):
        token = "ghu_" + "D" * 36
        content = f"auth={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitHub Token" for f in findings)

    def test_detects_github_token_ghr(self):
        token = "ghr_" + "E" * 36
        content = f"refresh={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitHub Token" for f in findings)

    def test_detects_github_pat_legacy(self):
        token = "github_pat_" + "F" * 22
        content = f"PAT={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitHub PAT (legacy)" for f in findings)

    def test_detects_gitlab_pat(self):
        token = "glpat-" + "a" * 20
        content = f"GITLAB_TOKEN={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitLab PAT" for f in findings)

    def test_gitlab_pat_with_hyphens_and_underscores(self):
        token = "glpat-" + "a_b-c" * 5
        content = f"token={token}"
        cleaned, findings = scan_and_strip_content(content)
        assert PLACEHOLDER in cleaned
        assert token not in cleaned
        assert any(f["pattern"] == "GitLab PAT" for f in findings)

    def test_match_truncated_to_40_chars(self):
        long_key = "sk-" + "a" * 60
        content = f"key = {long_key}"
        _, findings = scan_and_strip_content(content)
        assert len(findings) == 1
        assert len(findings[0]["match"]) <= 40


# ---------------------------------------------------------------------------
# scan_and_strip_credentials — tool integration tests (uses tmp git repo)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    # Initial commit so HEAD exists
    readme = tmp_path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


class TestScanAndStripCredentialsTool:
    """Integration tests using a real git repo."""

    def test_scans_modified_file_and_strips(self, git_repo: Path):
        secret_file = git_repo / "config.py"
        secret_file.write_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True, capture_output=True)

        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-1"
        )

        assert result["files_scanned"] >= 1
        assert result["files_modified"] >= 1
        assert len(result["findings"]) >= 1
        # Verify the file was actually cleaned
        assert PLACEHOLDER in secret_file.read_text()
        assert "AKIAIOSFODNN7EXAMPLE" not in secret_file.read_text()

    def test_no_modified_files_returns_zeros(self, git_repo: Path):
        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-2"
        )
        assert result["files_scanned"] == 0
        assert result["files_modified"] == 0
        assert result["findings"] == []

    def test_clean_file_not_modified(self, git_repo: Path):
        clean_file = git_repo / "clean.py"
        clean_file.write_text("x = 42\n")
        subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True, capture_output=True)

        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-3"
        )

        assert result["files_scanned"] >= 1
        assert result["files_modified"] == 0
        assert result["findings"] == []

    def test_untracked_files_also_scanned(self, git_repo: Path):
        untracked = git_repo / "leak.txt"
        untracked.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
        # Don't git add — file is untracked

        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-4"
        )

        assert result["files_scanned"] >= 1
        assert result["files_modified"] >= 1
        assert PLACEHOLDER in untracked.read_text()

    def test_findings_include_file_path(self, git_repo: Path):
        secret_file = git_repo / "secrets.env"
        secret_file.write_text('token = "sk-abcdefghijklmnopqrstuvwx"\n')
        subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True, capture_output=True)

        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-5"
        )

        assert len(result["findings"]) >= 1
        assert result["findings"][0]["file"] == "secrets.env"

    def test_multiple_files_with_mixed_content(self, git_repo: Path):
        (git_repo / "a.py").write_text("clean code\n")
        (git_repo / "b.py").write_text("key = AKIAIOSFODNN7EXAMPLE\n")
        (git_repo / "c.py").write_text("more clean code\n")
        subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True, capture_output=True)

        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-6"
        )

        assert result["files_scanned"] >= 3
        assert result["files_modified"] == 1

    def test_self_committed_secret_scanned_via_base_sha(self, git_repo: Path):
        """A secret in an agent self-commit is caught via base_sha..HEAD.

        Simulates OpenCode committing its own work: record the base tip,
        then write a file with a planted fake AWS key, ``git add`` and
        ``git commit`` it. With ``base_sha`` supplied the scanner diffs
        ``base_sha..HEAD`` and finds the committed file; without it, the
        committed (clean-tree) file is missed. The planted value is a
        synthetic test fixture, not a real credential.
        """
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(git_repo), check=True, capture_output=True, text=True,
        ).stdout.strip()

        fake_key = "AKIA" + "A" * 16
        committed_file = git_repo / "committed_secret.py"
        committed_file.write_text(f'AWS_KEY = "{fake_key}"\n')
        subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "agent self-commit"],
            cwd=str(git_repo), check=True, capture_output=True,
        )

        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-7", base_sha=base_sha
        )

        # The committed-since-base file is scanned and redacted.
        assert any(f["file"] == "committed_secret.py" for f in result["findings"])
        cleaned = committed_file.read_text()
        assert PLACEHOLDER in cleaned
        assert fake_key not in cleaned

    def test_scan_discovery_failure_raises(self, git_repo: Path):
        """A failed discovery command surfaces as an error, not an empty set.

        Fail-closed: if ``git diff``/``ls-files`` exits non-zero the scanner
        must raise rather than silently reporting zero files (which could let
        a broken scan look like a clean no-op).
        """
        with pytest.raises(subprocess.CalledProcessError):
            # A bogus base_sha makes ``git diff base..HEAD`` fail.
            _get_scan_files(str(git_repo), "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    def test_self_committed_secret_missed_without_base_sha(self, git_repo: Path):
        """Without base_sha, a clean-tree self-commit is not discovered.

        Documents the bug WI-2 fixes: a HEAD-only scan (legacy call) does
        not see files whose only changes are in commits already made,
        because the working tree is clean afterward.
        """
        fake_key = "AKIA" + "B" * 16
        committed_file = git_repo / "committed_secret2.py"
        committed_file.write_text(f'AWS_KEY = "{fake_key}"\n')
        subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "agent self-commit"],
            cwd=str(git_repo), check=True, capture_output=True,
        )

        # Legacy call without base_sha: clean tree, nothing discovered.
        result = scan_and_strip_credentials(
            work_dir=str(git_repo), job_id="test-job-8"
        )

        assert not any(
            f["file"] == "committed_secret2.py" for f in result["findings"]
        )
        # The file retains the planted value because it was never scanned.
        assert fake_key in committed_file.read_text()
