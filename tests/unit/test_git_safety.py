# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``container.lib.git_safety``."""

import os
import subprocess
from pathlib import Path

import pytest

from container.lib.git_safety import (
    GIT_HARDEN_ARGS,
    authenticated_remote_url,
    hardened_git_env,
    read_git_config,
    restore_git_config,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _git_config_value(repo: Path, key: str):
    out = subprocess.run(
        ["git", *GIT_HARDEN_ARGS, "config", "--get", key],
        cwd=str(repo), capture_output=True, text=True,
        env=hardened_git_env(),
    )
    return out.stdout.strip() or None


class TestRestoreGitConfig:
    def test_round_trip_drops_later_changes(self, repo: Path):
        snapshot = read_git_config(str(repo))
        subprocess.run(
            ["git", "config", "url.https://example.invalid/.insteadOf",
             "https://github.com/"],
            cwd=str(repo), check=True,
        )
        assert _git_config_value(repo, "url.https://example.invalid/.insteadof")
        restore_git_config(str(repo), snapshot)
        assert (repo / ".git" / "config").read_bytes() == snapshot
        assert _git_config_value(repo, "url.https://example.invalid/.insteadof") is None

    def test_include_path_removed(self, repo: Path, tmp_path_factory):
        snapshot = read_git_config(str(repo))
        included = tmp_path_factory.mktemp("inc") / "extra.cfg"
        included.write_text(
            '[url "https://example.invalid/"]\n\tinsteadOf = https://github.com/\n'
        )
        subprocess.run(
            ["git", "config", "include.path", str(included)],
            cwd=str(repo), check=True,
        )
        assert _git_config_value(repo, "url.https://example.invalid/.insteadof")
        restore_git_config(str(repo), snapshot)
        assert _git_config_value(repo, "include.path") is None
        assert _git_config_value(repo, "url.https://example.invalid/.insteadof") is None



class TestHardenedGitEnv:
    def test_config_files_off_and_inherited_git_config_dropped(self):
        env = hardened_git_env({
            "PATH": "/usr/bin",
            "GIT_CONFIG_COUNT": "9",
            "GIT_CONFIG_KEY_8": "x",
            "GIT_CONFIG_VALUE_8": "y",
            "GIT_CONFIG_PARAMETERS": "'core.hookspath'='/tmp'",
        })
        assert env["PATH"] == "/usr/bin"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ALLOW_PROTOCOL"] == "https"
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_8",
                    "GIT_CONFIG_VALUE_8", "GIT_CONFIG_PARAMETERS"):
            assert key not in env

    def test_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_TEST_MARKER", "1")
        assert hardened_git_env()["OPENCODE_TEST_MARKER"] == "1"

    def test_harden_args_win_over_repo_config(self, repo: Path):
        subprocess.run(
            ["git", "config", "core.hooksPath", "/somewhere"], cwd=str(repo), check=True
        )
        subprocess.run(
            ["git", "config", "credential.helper", "store"], cwd=str(repo), check=True
        )
        assert _git_config_value(repo, "core.hooksPath") == "/dev/null"
        assert _git_config_value(repo, "credential.helper") is None

    def test_global_config_ignored(self, repo: Path, tmp_path_factory, monkeypatch):
        home = tmp_path_factory.mktemp("home")
        (home / ".gitconfig").write_text("[opencode]\n\tmarker = global\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert _git_config_value(repo, "opencode.marker") is None

    def test_non_https_transport_refused(self, tmp_path: Path):
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        out = subprocess.run(
            ["git", "ls-remote", str(remote)],
            capture_output=True, text=True, env=hardened_git_env(),
        )
        assert out.returncode != 0
        assert "not allowed" in out.stderr


class TestAuthenticatedRemoteUrl:
    def test_https_url(self):
        assert (
            authenticated_remote_url("https://github.com/owner/repo")
            == "https://x-access-token@github.com/owner/repo"
        )

    def test_only_scheme_prefix_rewritten(self):
        assert (
            authenticated_remote_url("https://github.com/owner/https://repo")
            == "https://x-access-token@github.com/owner/https://repo"
        )

    @pytest.mark.parametrize("bad", [
        "git@github.com:owner/repo.git",
        "http://github.com/owner/repo",
        "https://user:pw@github.com/owner/repo",
        "https://x-access-token@github.com/owner/repo",
        "https://github.com/",
        "https://github.com",
        "https:///owner/repo",
        "HTTPS://github.com/owner/repo",
        "file:///tmp/repo",
        "ssh://github.com/owner/repo",
    ])
    def test_rejects_other_forms(self, bad: str):
        with pytest.raises(ValueError):
            authenticated_remote_url(bad)
