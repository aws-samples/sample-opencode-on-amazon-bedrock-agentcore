# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for git_clone tool.

Requirements: 1.1, 1.2, 1.3, 1.4
"""

import os
import subprocess
import sys
from unittest.mock import patch, MagicMock

# Stub strands before importing the module under test
strands_mock = MagicMock()
strands_mock.tool = lambda fn: fn  # @tool is identity decorator for testing
sys.modules.setdefault("strands", strands_mock)

from container.tools.git_clone import git_clone, _create_askpass_script


class TestCreateAskpassScript:
    """Test the _create_askpass_script helper."""

    def test_creates_file_that_exists(self):
        path = _create_askpass_script("test-token")
        try:
            assert os.path.exists(path)
        finally:
            sidecar = path + ".token"
            if os.path.exists(sidecar):
                os.remove(sidecar)
            os.remove(path)

    def test_script_contains_cat_sidecar(self):
        path = _create_askpass_script("my-secret-token")
        try:
            with open(path) as f:
                content = f.read()
            assert 'cat "$0.token"' in content
            assert content.startswith("#!/bin/sh\n")
        finally:
            sidecar = path + ".token"
            if os.path.exists(sidecar):
                os.remove(sidecar)
            os.remove(path)

    def test_script_is_owner_executable(self):
        import stat
        path = _create_askpass_script("tok")
        try:
            mode = os.stat(path).st_mode
            assert mode & stat.S_IRUSR  # owner read
            assert mode & stat.S_IXUSR  # owner execute
            assert not (mode & stat.S_IRGRP)  # no group read
            assert not (mode & stat.S_IROTH)  # no other read
        finally:
            sidecar = path + ".token"
            if os.path.exists(sidecar):
                os.remove(sidecar)
            os.remove(path)

    def test_script_has_sh_suffix(self):
        path = _create_askpass_script("tok")
        try:
            assert path.endswith(".sh")
        finally:
            sidecar = path + ".token"
            if os.path.exists(sidecar):
                os.remove(sidecar)
            os.remove(path)


class TestGitCloneAskpass:
    """Test that git_clone uses GIT_ASKPASS and does not embed token in URL."""

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_token_not_in_clone_url(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/owner/repo",
            token="ghp_test123",
            base_branch="main",
            work_dir="/tmp/work",
        )

        args = mock_run.call_args[0][0]
        for arg in args:
            assert "ghp_test123" not in arg

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_clone_url_has_username_only(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/owner/repo",
            token="ghp_test123",
            base_branch="main",
            work_dir="/tmp/work",
        )

        args = mock_run.call_args[0][0]
        assert "https://x-access-token@github.com/owner/repo" in args

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_git_askpass_env_set(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/owner/repo",
            token="tok",
            base_branch="main",
            work_dir="/tmp/work",
        )

        env = mock_run.call_args[1]["env"]
        assert env["GIT_ASKPASS"] == "/tmp/fake_askpass.sh"

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_https_only_transport_on_every_call(self, mock_remove, mock_exists, mock_askpass, mock_run):
        """GIT_ALLOW_PROTOCOL=https on the clone command."""
        git_clone(
            repo_url="https://github.com/owner/repo",
            token="tok",
            base_branch="main",
            work_dir="/tmp/work",
        )

        assert mock_run.call_count == 1
        assert mock_run.call_args[1]["env"]["GIT_ALLOW_PROTOCOL"] == "https"

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script")
    def test_unsupported_url_rejected_before_token_written(self, mock_askpass, mock_run):
        """A non-https or credential-bearing URL fails before the askpass
        script (and token sidecar) is created."""
        import pytest

        for bad in ("git@github.com:o/r.git", "https://u:p@github.com/o/r"):
            with pytest.raises(ValueError):
                git_clone(
                    repo_url=bad, token="tok", base_branch="main", work_dir="/tmp/work",
                )
        mock_askpass.assert_not_called()
        mock_run.assert_not_called()

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_askpass_script_cleaned_up(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/owner/repo",
            token="tok",
            base_branch="main",
            work_dir="/tmp/work",
        )

        assert mock_remove.call_count == 2
        removed_paths = [call[0][0] for call in mock_remove.call_args_list]
        assert "/tmp/fake_askpass.sh" in removed_paths
        assert "/tmp/fake_askpass.sh.token" in removed_paths

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_askpass_cleaned_up_on_error(self, mock_remove, mock_exists, mock_askpass, mock_run):
        import pytest

        cause = subprocess.CalledProcessError(128, "git")
        mock_run.side_effect = cause

        with pytest.raises(RuntimeError) as exc_info:
            git_clone(
                repo_url="https://github.com/owner/repo",
                token="tok",
                base_branch="main",
                work_dir="/tmp/work",
            )
        assert exc_info.value.__cause__ is cause

        assert mock_remove.call_count == 2
        removed_paths = [call[0][0] for call in mock_remove.call_args_list]
        assert "/tmp/fake_askpass.sh" in removed_paths
        assert "/tmp/fake_askpass.sh.token" in removed_paths


class TestGitCloneBasic:
    """Test the shallow clone command."""

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_shallow_clone_uses_depth_1(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/owner/repo",
            token="ghp_test123",
            base_branch="main",
            work_dir="/tmp/work",
        )

        mock_run.assert_called_once_with(
            ["git", "clone", "--depth", "1", "-b", "main",
             "https://x-access-token@github.com/owner/repo", "/tmp/work"],
            check=True, capture_output=True, env=mock_run.call_args[1]["env"],
        )

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_uses_check_true(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/o/r",
            token="t",
            base_branch="main",
            work_dir="/w",
        )

        assert mock_run.call_args[1]["check"] is True

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_uses_capture_output(self, mock_remove, mock_exists, mock_askpass, mock_run):
        git_clone(
            repo_url="https://github.com/o/r",
            token="t",
            base_branch="main",
            work_dir="/w",
        )

        assert mock_run.call_args[1]["capture_output"] is True

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_propagates_subprocess_error(self, mock_remove, mock_exists, mock_askpass, mock_run):
        """A failed clone surfaces as RuntimeError chained from the
        CalledProcessError (not the raw CalledProcessError repr)."""
        import pytest

        cause = subprocess.CalledProcessError(128, "git")
        mock_run.side_effect = cause

        with pytest.raises(RuntimeError) as exc_info:
            git_clone(
                repo_url="https://github.com/o/r",
                token="t",
                base_branch="main",
                work_dir="/w",
            )
        assert exc_info.value.__cause__ is cause


class TestGitCloneErrorMessages:
    """git clone failures are translated into user-facing messages."""

    def _run(self, mock_run, stderr, base_branch="main"):
        import pytest

        mock_run.side_effect = subprocess.CalledProcessError(128, "git", stderr=stderr)
        with pytest.raises(RuntimeError) as exc_info:
            git_clone(
                repo_url="https://github.com/owner/repo",
                token="ghp_secret",
                base_branch=base_branch,
                work_dir="/tmp/work",
            )
        return str(exc_info.value)

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_missing_base_branch(self, mock_remove, mock_exists, mock_askpass, mock_run):
        msg = self._run(
            mock_run,
            b"fatal: Remote branch test20261007a not found in upstream origin\n",
            base_branch="test20261007a",
        )
        assert msg == (
            "base_branch 'test20261007a' not found on remote "
            "https://github.com/owner/repo"
        )

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_repository_not_found(self, mock_remove, mock_exists, mock_askpass, mock_run):
        msg = self._run(mock_run, b"remote: Repository not found.\nfatal: repository not found\n")
        assert msg == (
            "repository https://github.com/owner/repo not found or access "
            "denied for the connected git account"
        )

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_authentication_failed_str_stderr(self, mock_remove, mock_exists, mock_askpass, mock_run):
        # stderr may already be str when text mode is used
        msg = self._run(mock_run, "fatal: Authentication failed for 'https://github.com/owner/repo/'\n")
        assert "not found or access denied" in msg

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_generic_failure_includes_exit_code_and_stderr_tail(
        self, mock_remove, mock_exists, mock_askpass, mock_run
    ):
        msg = self._run(mock_run, b"fatal: unable to access: Could not resolve host\n")
        assert msg == (
            "git clone failed (exit 128): fatal: unable to access: Could not resolve host"
        )

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_generic_failure_truncates_long_stderr(
        self, mock_remove, mock_exists, mock_askpass, mock_run
    ):
        msg = self._run(mock_run, b"x" * 1000)
        assert msg == "git clone failed (exit 128): " + "x" * 300

    @patch("container.tools.git_clone.subprocess.run")
    @patch("container.tools.git_clone._create_askpass_script", return_value="/tmp/fake_askpass.sh")
    @patch("container.tools.git_clone.os.path.exists", return_value=True)
    @patch("container.tools.git_clone.os.remove")
    def test_no_stderr(self, mock_remove, mock_exists, mock_askpass, mock_run):
        msg = self._run(mock_run, None)
        assert msg == "git clone failed (exit 128): "
