# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for git_push_and_create_pr tool.

Requirements: 2.1, 2.2, 2.3, 2.4, 15.1, 15.2
"""

import json
import logging
import subprocess as _sp
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Stub strands before importing the module under test
strands_mock = MagicMock()
strands_mock.tool = lambda fn: fn
sys.modules.setdefault("strands", strands_mock)

from container.tools.git_push_and_create_pr import git_push_and_create_pr


def _git_subcommand(cmd):
    """Return (subcommand, next_arg) for a git argv, skipping ``-c k=v``.

    The tool prepends hardening flags (``-c core.hooksPath=/dev/null`` ...)
    to every git invocation, so the subcommand is no longer at a fixed
    index. This helper walks past the leading ``git`` and any ``-c <value>``
    pairs to find the real subcommand (e.g. ``diff``, ``push``, ``rev-list``)
    and the token that follows it.
    """
    i = 1  # skip "git"
    while i < len(cmd) and cmd[i] == "-c":
        i += 2  # skip "-c" and its "key=value"
    sub = cmd[i] if i < len(cmd) else None
    nxt = cmd[i + 1] if i + 1 < len(cmd) else None
    return sub, nxt


def _make_subprocess_mock():
    """Create a subprocess.run mock that simulates successful git operations.

    By default ``diff --cached`` reports one staged file, so the tool
    proceeds to commit and push.
    """
    mock = MagicMock()
    diff_result = MagicMock()
    diff_result.stdout = "file.py | 1 +\n"

    def side_effect(cmd, **kwargs):
        sub, nxt = _git_subcommand(cmd)
        if sub == "diff" and nxt == "--cached":
            return diff_result
        return MagicMock()

    mock.side_effect = side_effect
    return mock


# A syntactically valid full commit id for the mock-based tests.
_BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _make_urlopen_response(body: dict):
    """Create a mock urlopen response returning JSON body."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    return resp


class TestPRCreationUsesUrllib:
    """Test that PR creation uses urllib.request instead of curl subprocess."""

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_no_curl_in_subprocess_calls(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        mock_urlopen.return_value = _make_urlopen_response(
            {"html_url": "https://github.com/o/r/pull/1"}
        )

        git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_secret",
            repo_url="https://github.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        for call_obj in mock_run.call_args_list:
            cmd = call_obj[0][0]
            assert cmd[0] != "curl", "curl should not be called as a subprocess"

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_token_not_in_subprocess_args(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        mock_urlopen.return_value = _make_urlopen_response(
            {"html_url": "https://github.com/o/r/pull/1"}
        )
        token = "ghp_supersecrettoken123456"

        git_push_and_create_pr(
            work_dir="/tmp/w",
            token=token,
            repo_url="https://github.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        for call_obj in mock_run.call_args_list:
            for arg in call_obj[0][0]:
                assert token not in arg

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_urlopen_called_with_correct_url(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        mock_urlopen.return_value = _make_urlopen_response(
            {"html_url": "https://github.com/myorg/myrepo/pull/42"}
        )

        git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_tok",
            repo_url="https://github.com/myorg/myrepo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.github.com/repos/myorg/myrepo/pulls"
        assert req.get_header("Authorization") == "Bearer ghp_tok"
        assert req.get_method() == "POST"


class TestPRCreationSuccessResponse:
    """Test successful PR creation returns html_url."""

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_returns_html_url(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        expected_url = "https://github.com/owner/repo/pull/99"
        mock_urlopen.return_value = _make_urlopen_response(
            {"html_url": expected_url, "id": 12345}
        )

        result = git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_tok",
            repo_url="https://github.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        assert result["pr_url"] == expected_url
        assert result["pushed"] is True

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_returns_none_when_no_html_url_in_response(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        mock_urlopen.return_value = _make_urlopen_response({"id": 12345})

        result = git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_tok",
            repo_url="https://github.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        assert result["pr_url"] is None
        assert result["pushed"] is True


class TestPRCreationErrorLogging:
    """Test that errors are logged at WARNING level and fallback is returned."""

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_http_error_logged_at_warning(self, mock_run, mock_urlopen, caplog):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        http_error = urllib.error.HTTPError(
            url="https://api.github.com/repos/o/r/pulls",
            code=422,
            msg="Unprocessable Entity",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"message":"Validation Failed"}')),
        )
        mock_urlopen.side_effect = http_error

        with caplog.at_level(logging.WARNING, logger="container.tools.git_push_and_create_pr"):
            result = git_push_and_create_pr(
                work_dir="/tmp/w",
                token="ghp_tok",
                repo_url="https://github.com/owner/repo",
                target_branch="feat",
                base_branch="main",
                task_description="task",
                job_id="j1",
            )

        assert result == {"pr_url": None, "pushed": True}
        assert any("HTTP error 422" in r.message for r in caplog.records)

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_url_error_logged_at_warning(self, mock_run, mock_urlopen, caplog):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with caplog.at_level(logging.WARNING, logger="container.tools.git_push_and_create_pr"):
            result = git_push_and_create_pr(
                work_dir="/tmp/w",
                token="ghp_tok",
                repo_url="https://github.com/owner/repo",
                target_branch="feat",
                base_branch="main",
                task_description="task",
                job_id="j1",
            )

        assert result == {"pr_url": None, "pushed": True}
        assert any("URL error" in r.message for r in caplog.records)

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_json_parse_error_logged_at_warning(self, mock_run, mock_urlopen, caplog):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        resp_mock = MagicMock()
        resp_mock.read.return_value = b"not valid json{{"
        mock_urlopen.return_value = resp_mock

        with caplog.at_level(logging.WARNING, logger="container.tools.git_push_and_create_pr"):
            result = git_push_and_create_pr(
                work_dir="/tmp/w",
                token="ghp_tok",
                repo_url="https://github.com/owner/repo",
                target_branch="feat",
                base_branch="main",
                task_description="task",
                job_id="j1",
            )

        assert result == {"pr_url": None, "pushed": True}
        assert any("parse" in r.message.lower() for r in caplog.records)

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_http_403_error_returns_fallback(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        http_error = urllib.error.HTTPError(
            url="https://api.github.com/repos/o/r/pulls",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"message":"rate limit"}')),
        )
        mock_urlopen.side_effect = http_error

        result = git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_tok",
            repo_url="https://github.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        assert result == {"pr_url": None, "pushed": True}


class TestPRCreationNonGitHubRepo:
    """Test behavior when repo URL doesn't match GitHub pattern."""

    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_non_github_url_returns_pushed_true_no_pr(self, mock_run):
        mock_run.side_effect = _make_subprocess_mock().side_effect

        result = git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_tok",
            repo_url="https://gitlab.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        assert result == {"pr_url": None, "pushed": True}


class TestPRCreationNoDiff:
    """Test behavior when there are no changes to commit."""

    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_no_changes_returns_not_pushed(self, mock_run):
        diff_result = MagicMock()
        diff_result.stdout = ""

        def side_effect(cmd, **kwargs):
            sub, nxt = _git_subcommand(cmd)
            if sub == "diff" and nxt == "--cached":
                return diff_result
            return MagicMock()

        mock_run.side_effect = side_effect

        result = git_push_and_create_pr(
            work_dir="/tmp/w",
            token="ghp_tok",
            repo_url="https://github.com/owner/repo",
            target_branch="feat",
            base_branch="main",
            task_description="task",
            job_id="j1",
        )

        assert result == {"pr_url": None, "pushed": False}


# ---------------------------------------------------------------------------
# WI-2: base_sha squash order, input validation, push URL and git hardening.
# ---------------------------------------------------------------------------


def _side_effect_with_staged(staged: str):
    """subprocess.run side_effect whose ``diff --cached`` prints ``staged``."""
    diff_result = MagicMock()
    diff_result.stdout = staged

    def side_effect(cmd, **kwargs):
        sub, nxt = _git_subcommand(cmd)
        if sub == "diff" and nxt == "--cached":
            return diff_result
        return MagicMock()

    return side_effect


def _subcommands(mock_run):
    return [_git_subcommand(c[0][0])[0] for c in mock_run.call_args_list]


def _call_push(**overrides):
    kwargs = dict(
        work_dir="/tmp/w",
        token="ghp_tok",
        repo_url="https://github.com/owner/repo",
        target_branch="feat",
        base_branch="main",
        task_description="task",
        job_id="j1",
        base_sha=_BASE_SHA,
    )
    kwargs.update(overrides)
    return git_push_and_create_pr(**kwargs)


class TestPushBaseShaSemantics:
    """reset --soft base_sha -> add -A -> empty check -> one commit."""

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_squash_order_and_single_commit(self, mock_run, mock_urlopen):
        mock_run.side_effect = _side_effect_with_staged("a.py | 1 +\n")
        mock_urlopen.return_value = _make_urlopen_response(
            {"html_url": "https://github.com/owner/repo/pull/7"}
        )

        result = _call_push()

        subs = _subcommands(mock_run)
        assert subs[:5] == ["reset", "add", "diff", "commit", "push"]
        assert subs.count("commit") == 1
        reset_argv = mock_run.call_args_list[0][0][0]
        assert reset_argv[-2:] == ["--soft", _BASE_SHA]
        assert result == {
            "pr_url": "https://github.com/owner/repo/pull/7", "pushed": True,
        }

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_no_net_change_after_reset_skips_commit_push_and_pr(
        self, mock_run, mock_urlopen
    ):
        # After the soft reset the index matches base_sha (e.g. the agent
        # committed and then reverted): nothing to push.
        mock_run.side_effect = _side_effect_with_staged("")

        result = _call_push()

        assert result == {"pr_url": None, "pushed": False}
        subs = _subcommands(mock_run)
        assert subs == ["reset", "add", "diff"]
        assert not mock_urlopen.called

    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_empty_check_runs_with_check_true(self, mock_run):
        mock_run.side_effect = _side_effect_with_staged("")

        _call_push()

        diff_call = next(
            c for c in mock_run.call_args_list
            if _git_subcommand(c[0][0])[0] == "diff"
        )
        assert diff_call[1].get("check") is True

    @pytest.mark.parametrize("bad_sha", [
        "",
        "HEAD",
        "HEAD~1",
        "base000",
        "-" + "a" * 39,
        "A" * 40,
        "a" * 39,
        "a" * 41,
        "a" * 40 + "\n",
    ])
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_invalid_base_sha_rejected_before_any_git_command(
        self, mock_run, bad_sha
    ):
        with pytest.raises(ValueError, match="base_sha"):
            _call_push(base_sha=bad_sha)
        mock_run.assert_not_called()

    @pytest.mark.parametrize("good_sha", ["a" * 40, "0" * 64])
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_sha1_and_sha256_ids_accepted(self, mock_run, good_sha):
        mock_run.side_effect = _side_effect_with_staged("")
        assert _call_push(base_sha=good_sha) == {"pr_url": None, "pushed": False}


class TestPushUrlAndHardening:
    """Push URL is derived from repo_url."""

    @patch("container.tools.git_push_and_create_pr.urllib.request.urlopen")
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_push_url_built_from_repo_url_not_origin(self, mock_run, mock_urlopen):
        mock_run.side_effect = _make_subprocess_mock().side_effect
        mock_urlopen.return_value = _make_urlopen_response(
            {"html_url": "https://github.com/owner/repo/pull/1"}
        )
        token = "ghp_supersecrettoken123456"

        _call_push(token=token)

        push_calls = [
            c for c in mock_run.call_args_list
            if _git_subcommand(c[0][0])[0] == "push"
        ]
        assert push_calls, "expected at least one push call"
        push_argv = push_calls[0][0][0]
        # URL derived from repo_url, with the x-access-token@ username.
        assert "https://x-access-token@github.com/owner/repo" in push_argv
        # The remote name "origin" is NOT used as the push target.
        assert "origin" not in push_argv
        # The token itself never appears in argv (it travels via askpass).
        for arg in push_argv:
            assert token not in arg

    @pytest.mark.parametrize("bad_url", [
        "git@github.com:owner/repo.git",
        "http://github.com/owner/repo",
        "https://user:pw@github.com/owner/repo",
        "https://token@github.com/owner/repo",
        "https://github.com/",
        "file:///tmp/repo",
        "HTTPS://github.com/owner/repo",
    ])
    @patch("container.tools.git_push_and_create_pr.subprocess.run")
    def test_unsupported_repo_url_rejected_before_any_git_command(
        self, mock_run, bad_url
    ):
        with pytest.raises(ValueError):
            _call_push(repo_url=bad_url)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Real-git tests.
#
# These use genuine local git repositories (a bare "remote" plus a working
# clone) so the squash, hook neutralization, URL derivation and the
# pipeline's .git/config restore are exercised end to end rather than via
# argv assertions. The validated HTTPS push URL is replaced by the local
# bare repository path, and the file transport is allowed for the test only
# (production allows HTTPS only).
# ---------------------------------------------------------------------------

from container.lib.git_safety import (
    GIT_HARDEN_ARGS,
    hardened_git_env,
    read_git_config,
    restore_git_config,
)
from container.tools.scan_and_strip_credentials import (
    scan_and_strip_credentials,
)

_GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]


def _git(cwd, *args, check=True):
    """Run a git command in *cwd* (inherited env), raising on failure."""
    return _sp.run(
        ["git", *_GIT_ID, *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path):
    """A bare 'remote' with one commit on main, and a working clone.

    Returns ``(work_dir, remote_dir, base_sha)``. The clone is on
    ``opencode/j1`` at ``base_sha``, as the pipeline leaves it before
    OpenCode runs.
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-b", "main", str(seed))
    (seed / "README.md").write_text("# seed\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", str(remote), "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-b", "main", str(remote), str(work))
    _git(work, "config", "user.name", "OpenCode")
    _git(work, "config", "user.email", "opencode@agentcore.aws")
    _git(work, "checkout", "-b", "opencode/j1")
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    return work, remote, base_sha


def _run_push(work: Path, remote: Path, base_sha: str):
    """Call the tool with the push URL pointed at the local remote."""
    with (
        patch(
            "container.tools.git_push_and_create_pr.authenticated_remote_url",
            return_value=str(remote),
        ),
        patch("container.lib.git_safety.ALLOWED_GIT_PROTOCOLS", "https:file"),
        patch(
            "container.tools.git_push_and_create_pr.urllib.request.urlopen",
            return_value=_make_urlopen_response(
                {"html_url": "https://github.com/owner/repo/pull/7"}
            ),
        ) as mock_urlopen,
    ):
        result = git_push_and_create_pr(
            work_dir=str(work),
            token="ghp_test",
            repo_url="https://github.com/owner/repo",
            target_branch="opencode/j1",
            base_branch="main",
            task_description="task",
            job_id="j1",
            base_sha=base_sha,
        )
    return result, mock_urlopen


def _remote_branch_sha(remote: Path, branch: str):
    out = _git(remote, "rev-parse", "--verify", "--quiet",
               f"refs/heads/{branch}", check=False)
    return out.stdout.strip() or None


class TestRealGitRepository:
    def test_no_changes_means_no_push_and_no_pr(self, repo):
        work, remote, base_sha = repo
        result, mock_urlopen = _run_push(work, remote, base_sha)
        assert result == {"pr_url": None, "pushed": False}
        mock_urlopen.assert_not_called()
        assert _remote_branch_sha(remote, "opencode/j1") is None

    def test_agent_commit_then_revert_means_no_push_and_no_pr(self, repo):
        work, remote, base_sha = repo
        (work / "feature.py").write_text("x = 1\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", "agent commit")
        _git(work, "revert", "--no-edit", "HEAD")
        result, mock_urlopen = _run_push(work, remote, base_sha)
        assert result == {"pr_url": None, "pushed": False}
        mock_urlopen.assert_not_called()
        assert _remote_branch_sha(remote, "opencode/j1") is None

    def test_agent_commit_is_pushed_as_one_commit(self, repo):
        work, remote, base_sha = repo
        (work / "feature.py").write_text("x = 1\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", "agent commit 1")
        (work / "feature.py").write_text("x = 2\n")
        _git(work, "commit", "-am", "agent commit 2")
        result, mock_urlopen = _run_push(work, remote, base_sha)
        assert result["pushed"] is True
        assert result["pr_url"] == "https://github.com/owner/repo/pull/7"
        mock_urlopen.assert_called_once()
        pushed = _remote_branch_sha(remote, "opencode/j1")
        assert _git(remote, "rev-parse", f"{pushed}^").stdout.strip() == base_sha
        assert _git(remote, "show", f"{pushed}:feature.py").stdout == "x = 2\n"

    def test_committed_secret_absent_from_pushed_history(self, repo):
        work, remote, base_sha = repo
        # Simulate the agent committing a file that contains a fake secret.
        fake_key = "AKIA" + "A" * 16
        (work / "leak.py").write_text(f'AWS_KEY = "{fake_key}"\n')
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "agent self-commit with secret")

        # Scanner redacts the working-tree copy (leaves the secret in the
        # original commit's blob).
        scan_and_strip_credentials(
            work_dir=str(work), job_id="j-scrub", base_sha=base_sha
        )
        result, _ = _run_push(work, remote, base_sha)

        assert result["pushed"] is True
        pushed = _remote_branch_sha(remote, "opencode/j1")
        history = _git(remote, "log", "-p", f"{base_sha}..{pushed}").stdout
        assert fake_key not in history
        assert "<REDACTED_SECRET>" in history

    def test_secret_removed_in_later_agent_commit_is_not_pushed(self, repo):
        work, remote, base_sha = repo
        (work / "leak.txt").write_text("AKIAIOSFODNN7EXAMPLE\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", "add file")
        _git(work, "rm", "-q", "leak.txt")
        (work / "ok.txt").write_text("ok\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", "replace file")
        _run_push(work, remote, base_sha)
        pushed = _remote_branch_sha(remote, "opencode/j1")
        history = _git(remote, "log", "-p", f"{base_sha}..{pushed}").stdout
        assert "AKIAIOSFODNN7EXAMPLE" not in history

    def test_hooks_in_repository_are_not_run(self, repo):
        work, remote, base_sha = repo
        marker = work.parent / "hook-ran"
        hook_body = f"#!/bin/sh\ntouch {marker}\nexit 1\n"
        hooks = work / ".git" / "hooks"
        for name in ("pre-commit", "commit-msg", "pre-push"):
            (hooks / name).write_text(hook_body)
            (hooks / name).chmod(0o755)
        custom = work.parent / "custom-hooks"
        custom.mkdir()
        (custom / "pre-push").write_text(hook_body)
        (custom / "pre-push").chmod(0o755)
        _git(work, "config", "core.hooksPath", str(custom))
        (work / "feature.py").write_text("x = 1\n")
        result, _ = _run_push(work, remote, base_sha)
        assert result["pushed"] is True
        assert not marker.exists()

    def test_rewritten_origin_url_is_ignored(self, repo, tmp_path):
        work, remote, base_sha = repo
        other = tmp_path / "other.git"
        _git(tmp_path, "init", "--bare", "-b", "main", str(other))
        _git(work, "remote", "set-url", "origin", str(other))
        _git(work, "remote", "set-url", "--push", "origin", str(other))
        (work / "feature.py").write_text("x = 1\n")
        _run_push(work, remote, base_sha)
        assert _remote_branch_sha(remote, "opencode/j1") is not None
        assert _remote_branch_sha(other, "opencode/j1") is None

    def test_file_transport_blocked_without_test_override(self, repo):
        """Production env allows HTTPS only, so a push to a local path
        fails even when the URL itself is accepted."""
        work, remote, base_sha = repo
        (work / "feature.py").write_text("x = 1\n")
        with patch(
            "container.tools.git_push_and_create_pr.authenticated_remote_url",
            return_value=str(remote),
        ):
            with pytest.raises(_sp.CalledProcessError):
                git_push_and_create_pr(
                    work_dir=str(work),
                    token="ghp_test",
                    repo_url="https://github.com/owner/repo",
                    target_branch="opencode/j1",
                    base_branch="main",
                    task_description="task",
                    job_id="j1",
                    base_sha=base_sha,
                )
        assert _remote_branch_sha(remote, "opencode/j1") is None


def _resolved_url(work: Path, url: str) -> str:
    """URL git would contact for ``url`` after applying insteadOf rules."""
    return _sp.run(
        ["git", *GIT_HARDEN_ARGS, "ls-remote", "--get-url", url],
        cwd=str(work), check=True, capture_output=True, text=True,
        env=hardened_git_env(),
    ).stdout.strip()


class TestRealGitConfigRestore:
    """The pipeline's .git/config snapshot/restore discards local config the
    agent added, including rules pulled in through ``include.path``."""

    def test_include_path_insteadof_discarded_and_push_reaches_original(
        self, repo, tmp_path
    ):
        work, remote, base_sha = repo
        other = tmp_path / "other.git"
        _git(tmp_path, "init", "--bare", "-b", "main", str(other))

        # Pipeline: snapshot after clone/config/checkout, before OpenCode.
        snapshot = read_git_config(str(work))

        # Agent: an include.path pointing at a file that defines insteadOf
        # rules, both for the GitHub prefix and for the local remote used
        # by this test.
        included = tmp_path / "included.cfg"
        included.write_text(
            f'[url "{other}/"]\n'
            "\tinsteadOf = https://github.com/\n"
            f'[url "{other}"]\n'
            f"\tinsteadOf = {remote}\n"
        )
        _git(work, "config", "--local", "include.path", str(included))
        (work / "feature.py").write_text("x = 1\n")

        # Control: before the restore the rules are in effect.
        assert _resolved_url(work, "https://github.com/owner/repo") == (
            f"{other}/owner/repo"
        )
        assert _resolved_url(work, str(remote)) == str(other)

        # Pipeline: restore after OpenCode, before scan and push.
        restore_git_config(str(work), snapshot)

        assert (work / ".git" / "config").read_bytes() == snapshot
        assert _resolved_url(work, "https://github.com/owner/repo") == (
            "https://github.com/owner/repo"
        )
        assert _resolved_url(work, str(remote)) == str(remote)

        result, _ = _run_push(work, remote, base_sha)

        assert result["pushed"] is True
        assert _remote_branch_sha(remote, "opencode/j1") is not None
        assert _remote_branch_sha(other, "opencode/j1") is None

    def test_direct_local_insteadof_discarded(self, repo, tmp_path):
        work, remote, base_sha = repo
        other = tmp_path / "other.git"
        _git(tmp_path, "init", "--bare", "-b", "main", str(other))
        snapshot = read_git_config(str(work))

        _git(work, "config", "--local", f"url.{other}.insteadOf", str(remote))
        _git(work, "config", "--local", f"url.{other}.pushInsteadOf", str(remote))
        (work / "feature.py").write_text("x = 1\n")

        restore_git_config(str(work), snapshot)
        result, _ = _run_push(work, remote, base_sha)

        assert result["pushed"] is True
        assert _remote_branch_sha(remote, "opencode/j1") is not None
        assert _remote_branch_sha(other, "opencode/j1") is None
