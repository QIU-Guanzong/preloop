"""Execute the real restore shell against unpublished and divergent Git heads."""

import os
import subprocess
from pathlib import Path

import pytest

from preloop.agents.checkpoint_client import capture, restore
from preloop.agents.container import ContainerAgentExecutor


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.mark.parametrize(
    "remote_state",
    ["missing", "behind", "equal", "diverged", "unavailable", "wrong_branch"],
)
def test_restore_preserves_unpublished_work(tmp_path: Path, remote_state: str) -> None:
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    git(upstream, "init", "--bare", "--initial-branch=main")
    source = tmp_path / "source"
    git(tmp_path, "clone", str(upstream), str(source))
    git(source, "config", "user.name", "Jane Doe")
    git(source, "config", "user.email", "jane@example.com")
    (source / "tracked").write_text("base")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    git(source, "push", "origin", "main")
    base = git(source, "rev-parse", "HEAD")
    branch = "implementation/test"
    git(source, "checkout", "-b", branch)
    (source / "tracked").write_text("unpushed")
    git(source, "commit", "-am", "unpushed")
    head = git(source, "rev-parse", "HEAD")
    if remote_state == "behind":
        git(source, "push", "origin", f"{base}:refs/heads/{branch}")
    elif remote_state == "equal":
        git(source, "push", "origin", branch)
    elif remote_state == "diverged":
        git(source, "checkout", "main")
        (source / "tracked").write_text("remote change")
        git(source, "commit", "-am", "remote change")
        git(source, "push", "origin", f"HEAD:refs/heads/{branch}")
        git(source, "checkout", branch)
    (source / "staged").write_text("staged work")
    git(source, "add", "staged")
    (source / "tracked").write_text("dirty work")
    (source / "untracked").write_text("required work")
    restored = tmp_path / "restored"
    restore(capture(source, max_bytes=2_000_000), restored)
    before = git(restored, "status", "--porcelain")
    executor = ContainerAgentExecutor("codex", {}, image="test:latest")
    context = {
        "checkpoint_env": {"PRELOOP_CHECKPOINT_GET_TOKEN": "test-capability"},
        "_git_source_branch": "implementation/other"
        if remote_state == "wrong_branch"
        else branch,
        "git_clone_config": {
            "enabled": True,
            "repositories": [
                {
                    "repository_url": str(
                        tmp_path / "absent"
                        if remote_state == "unavailable"
                        else upstream
                    ),
                    "clone_path": str(restored),
                }
            ],
        },
    }
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -e\n"
            + executor._wrap_clone_with_workspace_restore("exit 99", context),
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        },
        capture_output=True,
        text=True,
    )
    assert git(restored, "rev-parse", "HEAD") == head
    assert git(restored, "status", "--porcelain") == before
    assert (restored / "tracked").read_text() == "dirty work"
    assert (restored / "untracked").read_text() == "required work"
    if remote_state in {"diverged", "unavailable", "wrong_branch"}:
        assert result.returncode != 0
        expected = {
            "diverged": "remote_diverged",
            "unavailable": "remote_unavailable",
            "wrong_branch": "branch_mismatch",
        }[remote_state]
        assert expected in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        if remote_state == "missing":
            assert "remote_branch_absent" in result.stdout


def test_direct_restore_does_not_clone_when_repository_is_missing(
    tmp_path: Path,
) -> None:
    executor = ContainerAgentExecutor("codex", {}, image="test:latest")
    script = executor._wrap_clone_with_workspace_restore(
        "echo COLD_CLONE",
        {
            "checkpoint_env": {"PRELOOP_CHECKPOINT_GET_TOKEN": "test-capability"},
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "clone_path": str(tmp_path / "absent"),
                        "repository_url": "https://example.com/team/repo",
                    }
                ],
            },
        },
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert "repository_missing" in result.stdout
    assert "COLD_CLONE" not in result.stdout
