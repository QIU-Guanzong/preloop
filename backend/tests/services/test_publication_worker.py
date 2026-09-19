"""Exercise frozen helper boundaries using real local Git bundles."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from preloop.services.publication_worker import (
    freeze_publication,
    inspect_bundle,
    read_regular_file,
)
from preloop.services.trusted_publisher import PublicationError


@pytest.fixture
def candidate(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    git("init")
    git("config", "user.name", "Local test")
    git("config", "user.email", "test@example.invalid")
    (repository / "initial.txt").write_text("base")
    git("add", ".")
    git("commit", "-m", "Base")
    base = git("rev-parse", "HEAD")
    (repository / "test with spaces.py").write_text("pass\n")
    git("add", ".")
    git("commit", "-m", "Implementation")
    source = tmp_path / "source"
    source.mkdir()
    git("bundle", "create", str(source / "branch.bundle"), "HEAD")
    return source, base, git("rev-parse", "HEAD"), git


def test_freeze_derives_real_commit_and_preserves_independent_bytes(
    candidate, tmp_path
):
    source, base, head, _ = candidate
    destination = tmp_path / "frozen"
    original = (source / "branch.bundle").read_bytes()
    manifest = freeze_publication(source, destination, base)
    assert manifest["head_sha"] == head
    assert manifest["changed_files"] == ["test with spaces.py"]
    assert manifest["bundle_sha256"] == hashlib.sha256(original).hexdigest()
    (source / "branch.bundle").write_bytes(b"agent rewrites source")
    assert (destination / "branch.bundle").read_bytes() == original
    assert (destination / "branch.bundle").stat().st_mode & 0o222 == 0


def test_read_regular_file_closes_fd_when_fdopen_raises_base_exception(
    tmp_path, monkeypatch
):
    (tmp_path / "branch.bundle").write_bytes(b"payload")
    closed: list[int] = []
    real_close = os.close
    file_fd: dict[str, int] = {}

    def close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def fdopen(fd: int, mode: str):
        file_fd["fd"] = fd
        raise KeyboardInterrupt

    monkeypatch.setattr("preloop.services.publication_worker.os.close", close)
    monkeypatch.setattr("preloop.services.publication_worker.os.fdopen", fdopen)
    with pytest.raises(KeyboardInterrupt):
        read_regular_file(tmp_path, "branch.bundle", 10)
    assert file_fd["fd"] in closed


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "oversize"])
def test_fixed_input_rejects_filesystem_aliases_and_special_files(tmp_path, kind):
    target = tmp_path / "branch.bundle"
    other = tmp_path / "other"
    other.write_bytes(b"contents")
    if kind == "symlink":
        target.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"x" * 20)
    with pytest.raises((OSError, PublicationError)):
        read_regular_file(tmp_path, "branch.bundle", 10)


def test_freeze_rejects_missing_base_and_ambiguous_head(candidate, tmp_path):
    source, base, _, git = candidate
    bundle = (source / "branch.bundle").read_bytes()
    with pytest.raises(PublicationError):
        inspect_bundle(bundle, "a" * 40)
    git("bundle", "create", str(source / "branch.bundle"), "--all")
    with pytest.raises(PublicationError, match="exactly one HEAD"):
        freeze_publication(source, tmp_path / "frozen", base)


def test_freeze_never_reuses_existing_destination(candidate, tmp_path):
    source, base, _, _ = candidate
    destination = tmp_path / "frozen"
    destination.mkdir()
    (destination / "branch.bundle").write_bytes(b"previous attempt")
    with pytest.raises(FileExistsError):
        freeze_publication(source, destination, base)
    assert (destination / "branch.bundle").read_bytes() == b"previous attempt"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("PRELOOP_PUBLICATION_DOCKER_IMAGE"),
    reason="Explicit local Docker fixture image required",
)
async def test_real_docker_checks_run_fresh_without_credentials_and_fail_closed(
    candidate,
):
    from types import SimpleNamespace
    from uuid import uuid4
    from preloop.agents.container import ContainerAgentExecutor
    from preloop.services.publication_hosted_verifier import verify_hosted_publication
    from preloop.services.verification import resolve_verification_policy

    source, base, head, _ = candidate
    executor = ContainerAgentExecutor(
        "codex", {}, os.environ["PRELOOP_PUBLICATION_DOCKER_IMAGE"]
    )
    checks = [
        {
            "id": "range",
            "command": f'test "$PRELOOP_VERIFY_BASE" = {base} && test "$PRELOOP_VERIFY_HEAD" = {head} && git diff --check "$PRELOOP_VERIFY_BASE...$PRELOOP_VERIFY_HEAD"',
            "reason": "controller-pinned published range",
        },
        {
            "id": "first",
            "command": 'test -z "$GITHUB_TOKEN$PRELOOP_API_TOKEN" && test ! -e /var/run/docker.sock && echo changed > "test with spaces.py"',
            "reason": "isolation",
        },
        {
            "id": "second",
            "command": 'test "$(cat "test with spaces.py")" = pass',
            "reason": "fresh checkout",
        },
    ]
    policy = SimpleNamespace(
        execution_id=str(uuid4()),
        base_sha=base,
        verification_image=executor.image,
        verification_policy=resolve_verification_policy(
            {
                "verification": {
                    "mode": "gate",
                    "profile": {
                        "profile_id": "local",
                        "version": "v1",
                        "always": checks,
                    },
                }
            }
        ),
    )
    try:
        result = await verify_hosted_publication(
            executor, policy, (source / "branch.bundle").read_bytes()
        )
        assert result.verification.head_sha == head
        assert [check["exit_code"] for check in result.checks] == [0, 0, 0]
        assert result.manifest["base_sha"] == base
        assert result.manifest["profile_id"] == "local"
        assert result.manifest["profile_version"] == "v1"
        assert len(result.manifest["profile_sha256"]) == 64
        assert result.manifest["environment"] == {
            "image": executor.image,
            "telemetry_disabled": True,
            "runtime": "docker",
        }
        assert result.checks[0]["selected_by"] == ["always"]
        assert result.checks[0]["reason"] == "controller-pinned published range"
        from unittest.mock import AsyncMock, patch

        with patch(
            "preloop.services.publication_hosted_verifier._check_docker",
            new=AsyncMock(),
        ) as check_again:
            reused = await verify_hosted_publication(
                executor, policy, (source / "branch.bundle").read_bytes()
            )
        check_again.assert_not_awaited()
        assert all(row["reused"] for row in reused.checks)
        policy.verification_policy.profile.always[0].command = "exit 7"
        with pytest.raises(PublicationError, match="exit 7"):
            await verify_hosted_publication(
                executor, policy, (source / "branch.bundle").read_bytes()
            )
        # A database dependency must exist inside the isolated check runtime.
        # An operator-owned bounded setup can provision it in the same command.
        database_check = "python3 -c 'import sqlite3; sqlite3.connect(\"file:/tmp/checks.sqlite?mode=rw\", uri=True)'"
        policy.verification_policy.profile.always[0].command = database_check
        with pytest.raises(PublicationError, match="failed with exit 1"):
            await verify_hosted_publication(
                executor, policy, (source / "branch.bundle").read_bytes()
            )
        policy.verification_policy.profile.always[0].command = (
            "python3 -c 'import sqlite3; sqlite3.connect(\"/tmp/checks.sqlite\").close()' && "
            + database_check
        )
        repaired_setup = await verify_hosted_publication(
            executor, policy, (source / "branch.bundle").read_bytes()
        )
        assert all(row["exit_code"] == 0 for row in repaired_setup.checks)
        docker = await executor._get_docker_client()
        residual = await docker.containers.list(
            all=True, filters={"label": [f"preloop.execution_id={policy.execution_id}"]}
        )
        assert residual == []
    finally:
        await executor.cleanup()


@pytest.mark.asyncio
async def test_publish_wire_fixture_rejects_digest_before_using_lease(candidate):
    import json
    from unittest.mock import AsyncMock, patch
    from preloop.services.publication_worker import publish_frozen

    source, _, _, _ = candidate
    request = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/publication_publish_request.json"
        ).read_text()
    )
    with (
        patch(
            "preloop.services.publication_worker.publish_verified_bundle",
            new=AsyncMock(),
        ) as publish,
        patch(
            "preloop.services.publication_worker.revoke_repository_lease",
            new=AsyncMock(),
        ) as revoke,
    ):
        with pytest.raises(PublicationError, match="digest changed"):
            await publish_frozen(source, request)
    publish.assert_not_awaited()
    revoke.assert_awaited_once()
    assert revoke.call_args.args[0].token == "fixture-test-token"


@pytest.mark.asyncio
async def test_publish_wire_fixture_binds_exact_bytes_and_revokes_on_success(candidate):
    import json
    from unittest.mock import AsyncMock, patch
    from preloop.services.publication_worker import publish_frozen

    source, _, head, _ = candidate
    request = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/publication_publish_request.json"
        ).read_text()
    )
    bundle = (source / "branch.bundle").read_bytes()
    request["bundle_sha256"] = hashlib.sha256(bundle).hexdigest()
    request["binding"]["head_sha"] = head
    request["binding"]["records"][0]["head_sha"] = head

    async def publish(**kwargs):
        assert kwargs["bundle"] == bundle
        assert kwargs["binding"].head_sha == head
        lease = await kwargs["acquire_lease"]()
        assert lease.token == "fixture-test-token"
        return {"url": "https://github.com/example/project/pull/1"}

    with (
        patch(
            "preloop.services.publication_worker.publish_verified_bundle", new=publish
        ),
        patch(
            "preloop.services.publication_worker.revoke_repository_lease",
            new=AsyncMock(),
        ) as revoke,
    ):
        result = await publish_frozen(source, request)
    assert result["url"].endswith("/pull/1")
    revoke.assert_awaited_once()


def test_changed_path_preserves_leading_whitespace_for_check_selection(candidate):
    source, base, _, git = candidate
    git("mv", "test with spaces.py", " leading.py")
    git("commit", "-m", "Leading whitespace filename")
    git("bundle", "create", str(source / "branch.bundle"), "HEAD")
    manifest = inspect_bundle((source / "branch.bundle").read_bytes(), base)
    assert manifest["changed_files"] == [" leading.py"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("PRELOOP_PUBLICATION_DOCKER_IMAGE"),
    reason="Explicit local Docker fixture image required",
)
@pytest.mark.parametrize("repair", [False, True])
async def test_isolated_fail_repair_verify_publish_with_fake_provider(
    candidate, repair
):
    """Real immutable checkouts, real publisher import, fake remote writes only."""
    import io
    import json
    import tarfile
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    import httpx

    from preloop.agents.container import ContainerAgentExecutor
    from preloop.services.isolated_publication import (
        IsolatedPublicationPolicy,
        finish_isolated_publication,
    )
    from preloop.services.publication_hosted_verifier import (
        HostedVerificationError,
        verify_hosted_publication,
    )
    from preloop.services.publication_verification import VerifiedPublication
    from preloop.services.trusted_publisher import CleanGitRepository, PublicationLease
    from preloop.services.verification import resolve_verification_policy

    source, base, _, git = candidate
    executor = ContainerAgentExecutor(
        "codex", {}, os.environ["PRELOOP_PUBLICATION_DOCKER_IMAGE"]
    )
    policy = IsolatedPublicationPolicy(
        tracker_id="tracker",
        account_id="account",
        repository_url="https://github.com/example/project.git",
        branch="preloop/issue-1",
        base="main",
        expected_remote_sha=base if repair else None,
        execution_id=str(uuid4()),
        previous_records=(),
        read_lease=None,
        configured_title="Repair selected behavior",
        configured_body="Local fixture",
        issue_number="1",
        base_sha=base,
        verification_image=executor.image,
        verification_policy=resolve_verification_policy(
            {
                "verification": {
                    "mode": "gate",
                    "profile": {
                        "profile_id": "lifecycle",
                        "version": "v1",
                        "always": [
                            {
                                "id": "invariant",
                                "command": 'git diff --check "$PRELOOP_VERIFY_BASE...$PRELOOP_VERIFY_HEAD" && test "$PRELOOP_DISABLE_TELEMETRY" = true && test -z "$GITHUB_TOKEN$PRELOOP_API_TOKEN"',
                                "reason": "always required",
                            }
                        ],
                        "rules": [
                            {
                                "id": "focused",
                                "path_globs": ["*.py"],
                                "description": "changed behavior",
                                "commands": [
                                    {
                                        "id": "regression",
                                        "command": 'test "$(cat "test with spaces.py")" = repaired',
                                        "reason": "focused regression",
                                    }
                                ],
                            },
                            {
                                "id": "frontend",
                                "path_globs": ["frontend/**"],
                                "description": "unrelated browser suite",
                                "commands": [
                                    {
                                        "id": "browser",
                                        "command": "exit 99",
                                        "reason": "unrelated",
                                    }
                                ],
                            },
                        ],
                        "unknown_default": [
                            {
                                "id": "unknown",
                                "command": "exit 98",
                                "reason": "fail unknown changes",
                            }
                        ],
                    },
                }
            }
        ),
    )
    writes = []
    minted = AsyncMock(
        return_value=PublicationLease(
            "fake-write",
            policy.repository_url,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    revoked = AsyncMock()

    def provider(request):
        assert request.url.host == "api.github.com"
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert request.method == "POST"
        writes.append("create")
        return httpx.Response(
            201,
            json={
                "number": 1,
                "html_url": "https://github.com/example/project/pull/1",
                "body": json.loads(request.content)["body"],
            },
        )

    def push(repo, binding, lease):
        assert repo.run("rev-parse", binding.head_sha) == binding.head_sha
        lease.validate(binding)
        writes.append("push")

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    async def publish(bundle, evidence):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("branch.bundle")
            member.size = len(bundle)
            archive.addfile(member, io.BytesIO(bundle))
        with (
            patch(
                "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
                return_value=SimpleNamespace(id="tracker"),
            ),
            patch(
                "preloop.services.isolated_publication.require_human_publication_approval"
            ),
            patch(
                "preloop.services.isolated_publication.mint_repository_lease",
                new=minted,
            ),
            patch(
                "preloop.services.isolated_publication.revoke_repository_lease",
                new=revoked,
            ),
            patch("preloop.services.isolated_publication.httpx.AsyncClient") as factory,
            patch.object(CleanGitRepository, "publish", new=push),
        ):
            factory.return_value.__aenter__.return_value = client
            return await finish_isolated_publication(
                None,
                policy,
                {"result": {"status": "success"}},
                buffer.getvalue(),
                evidence,
            )

    try:
        failed_bundle = (source / "branch.bundle").read_bytes()
        with pytest.raises(
            HostedVerificationError, match="regression failed"
        ) as failed:
            await verify_hosted_publication(executor, policy, failed_bundle)
        assert [row["id"] for row in failed.value.evidence["checks"]] == [
            "invariant",
            "regression",
        ]
        assert failed.value.evidence["checks"][-1]["exit_code"] == 1
        with pytest.raises(PublicationError):
            await publish(failed_bundle, None)
        assert writes == []
        minted.assert_not_awaited()

        repo_path = Path(git("rev-parse", "--show-toplevel"))
        (repo_path / "test with spaces.py").write_text("repaired\n")
        git("add", "test with spaces.py")
        git("commit", "-m", "Repair selected regression")
        git("bundle", "create", str(source / "branch.bundle"), "HEAD")
        repaired_bundle = (source / "branch.bundle").read_bytes()
        verified = await verify_hosted_publication(executor, policy, repaired_bundle)
        assert [row["id"] for row in verified.checks] == ["invariant", "regression"]
        evidence = verified.verification
        for invalid in (
            None,
            {"status": "passed", "head_sha": evidence.head_sha},
            replace(evidence, execution_id=str(uuid4())),
            replace(evidence, head_sha="f" * 40),
            VerifiedPublication(
                policy.execution_id,
                evidence.head_sha,
                hashlib.sha256(failed_bundle).hexdigest(),
            ),
        ):
            with pytest.raises(PublicationError):
                await publish(repaired_bundle, invalid)
            assert writes == []
            minted.assert_not_awaited()
        result = await publish(repaired_bundle, evidence)
        assert result["head_sha"] == git("rev-parse", "HEAD")
        assert writes == ["push", "create"]
        minted.assert_awaited_once()
        revoked.assert_awaited_once()
    finally:
        await client.aclose()
        await executor.cleanup()
