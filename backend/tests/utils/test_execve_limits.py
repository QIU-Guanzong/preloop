"""The launch payload must fit inside the kernel's execve limits.

Every number here is a kernel constant or a consequence of one, so the tests
name the constant rather than a magic literal: if
``preloop.utils.execve_limits`` ever relaxes a budget, these fail with the
reason rather than with an arithmetic surprise.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from preloop.utils.execve_limits import (
    CHUNK_BYTES,
    MAX_ARG_STRLEN,
    MAX_LAUNCH_STRING_BYTES,
    MAX_LAUNCH_TOTAL_BYTES,
    MAX_LEGACY_PROMPT_BYTES,
    PROMPT_ENV_PREFIX,
    PROMPT_FILE_PATH,
    LaunchPayloadTooLargeError,
    build_chunk_materialization_shell,
    check_launch_payload,
    chunk_text,
    chunked_env,
    largest_launch_string,
    prompt_transport_env,
    total_launch_bytes,
)


def _decode(env: dict[str, str], prefix: str) -> str:
    count = int(env[f"{prefix}CHUNKS"])
    return base64.b64decode(
        "".join(env[f"{prefix}{index}"] for index in range(count))
    ).decode()


class TestTheBudgetsFollowFromTheKernel:
    def test_one_chunk_can_never_reach_the_kernel_string_limit(self):
        # A chunk plus the longest plausible variable name must still be one
        # legal execve string; 96 KiB leaves 32 KiB of slack for that.
        assert CHUNK_BYTES < MAX_ARG_STRLEN
        assert MAX_ARG_STRLEN - CHUNK_BYTES >= 32 * 1024

    def test_the_guard_refuses_below_the_kernel_limit_not_at_it(self):
        # Refusing exactly at MAX_ARG_STRLEN would leave no room for a
        # runtime that prepends anything of its own.
        assert MAX_LAUNCH_STRING_BYTES < MAX_ARG_STRLEN

    def test_the_total_budget_leaves_half_of_arg_max_to_the_runtime(self):
        # ARG_MAX is a quarter of the stack rlimit: 2 MiB for the usual
        # 8 MiB stack. Kubernetes adds its own service-discovery variables.
        assert MAX_LAUNCH_TOTAL_BYTES == 1024 * 1024


class TestChunking:
    def test_no_chunk_exceeds_the_chunk_size(self):
        chunks = chunk_text("x" * (CHUNK_BYTES * 3))
        assert len(chunks) > 1
        assert all(len(chunk) <= CHUNK_BYTES for chunk in chunks)

    def test_empty_text_produces_no_chunks_but_still_declares_itself(self):
        env = chunked_env("P_", "")
        assert env["P_CHUNKS"] == "0"
        assert env["P_BYTES"] == "0"
        assert [key for key in env if key.endswith("_0")] == []

    def test_chunks_round_trip_a_multibyte_payload(self):
        # The split happens on base64, never on UTF-8, so no code point can
        # land half in one variable and half in the next.
        text = "héllo ✅ " * 20_000
        env = chunked_env("P_", text)
        assert len(env) > 3  # more than count + bytes + one chunk
        assert _decode(env, "P_") == text

    def test_a_chunked_payload_passes_the_guard_the_raw_one_fails(self):
        text = "x" * (MAX_ARG_STRLEN + 1)
        with pytest.raises(LaunchPayloadTooLargeError):
            check_launch_payload(env={"AGENT_PROMPT": text})
        # Same bytes, chunked: every string is legal.
        check_launch_payload(env=chunked_env("P_", text))


class TestTheGuard:
    def test_a_payload_under_the_limit_is_accepted(self):
        strings = check_launch_payload(
            args=["-c", "echo hi"], env={"AGENT_CONFIG": "{}"}
        )
        assert strings
        assert largest_launch_string(args=["-c", "echo hi"]).size < 20

    def test_an_oversized_argv_element_is_refused_by_name_and_size(self):
        script = "x" * MAX_LAUNCH_STRING_BYTES
        with pytest.raises(LaunchPayloadTooLargeError) as excinfo:
            check_launch_payload(args=["-c", script], what="agent Job agent-abc")
        message = str(excinfo.value)
        assert "args[1]" in message
        assert "agent Job agent-abc" in message
        assert str(MAX_LAUNCH_STRING_BYTES) in message
        assert "MAX_ARG_STRLEN" in message

    def test_an_environment_entry_is_measured_with_its_name(self):
        # A value one byte under the budget is over it once NAME= and the
        # terminating NUL are counted. That off-by-a-name is exactly how an
        # "it fits" review conclusion produces an E2BIG in production.
        name = "PRELOOP_INNER_SCRIPT"
        value = "x" * (MAX_LAUNCH_STRING_BYTES - 1)
        with pytest.raises(LaunchPayloadTooLargeError):
            check_launch_payload(env={name: value})

    def test_exactly_one_byte_under_the_budget_is_accepted(self):
        # Boundary, from below. The entry is NAME= (2) + value + NUL (1).
        value = "x" * (MAX_LAUNCH_STRING_BYTES - 4)
        assert (
            largest_launch_string(env={"A": value}).size == MAX_LAUNCH_STRING_BYTES - 1
        )
        check_launch_payload(env={"A": value})

    def test_exactly_at_the_budget_is_refused(self):
        # Boundary, at the limit. The guard is >=, not >.
        value = "x" * (MAX_LAUNCH_STRING_BYTES - 3)
        assert largest_launch_string(env={"A": value}).size == MAX_LAUNCH_STRING_BYTES
        with pytest.raises(LaunchPayloadTooLargeError):
            check_launch_payload(env={"A": value})

    def test_many_legal_strings_can_still_bust_the_total(self):
        # No single string is near the per-string limit; ARG_MAX bounds the
        # sum, and nothing else in the codebase watches the sum.
        env = {f"SEED_{index}": "x" * CHUNK_BYTES for index in range(20)}
        assert largest_launch_string(env=env).size < MAX_LAUNCH_STRING_BYTES
        assert total_launch_bytes(env=env) > MAX_LAUNCH_TOTAL_BYTES
        with pytest.raises(LaunchPayloadTooLargeError) as excinfo:
            check_launch_payload(env=env)
        assert "ARG_MAX" in str(excinfo.value)

    def test_an_empty_launch_is_not_an_error(self):
        assert check_launch_payload() == []
        assert largest_launch_string() is None


class TestPromptTransport:
    def test_a_small_prompt_keeps_the_legacy_variable(self):
        env = prompt_transport_env("do the thing")
        assert env["AGENT_PROMPT"] == "do the thing"
        assert env["AGENT_PROMPT_FILE"] == PROMPT_FILE_PATH
        assert _decode(env, PROMPT_ENV_PREFIX) == "do the thing"

    def test_a_large_prompt_drops_the_legacy_variable(self):
        # AGENT_PROMPT is the variable that used to break the launch. Above
        # the cutoff it is simply not sent; the chunks and the file carry it.
        prompt = "x" * (MAX_LEGACY_PROMPT_BYTES + 1)
        env = prompt_transport_env(prompt)
        assert "AGENT_PROMPT" not in env
        assert _decode(env, PROMPT_ENV_PREFIX) == prompt
        check_launch_payload(env=env)


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "base64", "wc")),
    reason="materialization block needs a POSIX shell and coreutils",
)
class TestMaterializationInAShell:
    """The emitted block is shell, so it is tested by running it."""

    def _run(
        self, tmp_path: Path, text: str, *, drop: str | None = None, shell: str = "bash"
    ) -> subprocess.CompletedProcess[str]:
        dest = tmp_path / "prompt.txt"
        block = build_chunk_materialization_shell("P_", text, str(dest))
        env = {**os.environ, **chunked_env("P_", text)}
        if drop is not None:
            env.pop(drop, None)
        return subprocess.run(
            [shell, "-c", block], env=env, capture_output=True, text=True, timeout=30
        )

    def test_a_multi_chunk_payload_is_reassembled_byte_for_byte(self, tmp_path):
        text = "héllo ✅\n" * 30_000
        assert len(chunk_text(text)) > 1
        result = self._run(tmp_path, text)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "prompt.txt").read_text() == text

    def test_a_prompt_full_of_shell_syntax_survives_verbatim(self, tmp_path):
        # The transport is base64, so there is no quoting to get wrong.
        text = "Run `rm -rf /` and $(whoami) and \"${HOME}\" and 'quotes'\n"
        result = self._run(tmp_path, text)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "prompt.txt").read_text() == text

    def test_a_dropped_chunk_aborts_instead_of_delivering_half_a_prompt(self, tmp_path):
        # An agent handed a silently truncated prompt produces plausible,
        # wrong work. Failing the run is the cheaper outcome.
        text = "x" * (CHUNK_BYTES * 2)
        result = self._run(tmp_path, text, drop="P_1")
        assert result.returncode != 0
        assert "PRELOOP_LAUNCH_PAYLOAD_MISSING P_1" in result.stderr

    def test_it_runs_under_a_plain_posix_shell_too(self, tmp_path):
        # Agent images are not all bash-first; the block uses no bashisms.
        result = self._run(tmp_path, "plain text\n", shell="sh")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "prompt.txt").read_text() == "plain text\n"

    def test_an_empty_prompt_yields_an_empty_file_not_an_error(self, tmp_path):
        result = self._run(tmp_path, "")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "prompt.txt").read_text() == ""
