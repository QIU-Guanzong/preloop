"""Placeholder parsing for flow prompt templates, with a truncation filter.

A prompt template interpolates trigger-payload fields verbatim:
``{{trigger_event.payload.object_attributes.description}}`` is whatever the
webhook carried, and a webhook carries whatever the sender wrote. A
Dependabot pull request that groups three npm packages puts every package's
release notes in its body: 34 KiB of HTML for preloop/preloop#609, and
nothing bounds it.

That text is then paid for three times: in the model's context window, in
the launch payload the kernel must accept (see
:mod:`preloop.utils.execve_limits`), and in the reviewer's attention. The
first is money, the second used to be a failed execution, and the third is
the reason a reviewer agent reads a diff rather than a changelog.

So a template may now bound what it injects::

    - Description: {{trigger_event.payload.object_attributes.description|truncate(16384)}}

``truncate`` caps the interpolated value at a byte count (default
:data:`DEFAULT_TRUNCATE_BYTES`) and appends a marker naming the full size, so
the agent can see that it is reading a prefix and go fetch the rest through
the tool it already has. Bytes, not characters: the limits downstream are
byte limits, and a character count would under-count any non-ASCII body.

Templates without a filter are unaffected; the grammar is otherwise the one
the orchestrator has always used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# 16 KiB is roughly 4k tokens: enough that a normal PR description, an issue
# body or a commit message arrives whole, small enough that a generated
# changelog cannot dominate the prompt.
DEFAULT_TRUNCATE_BYTES = 16 * 1024

# ``{{name.path}}`` with an optional ``|truncate`` or ``|truncate(N)``.
# Whitespace is tolerated around the name and the filter so a template author
# is not punished for writing ``{{ x | truncate(1024) }}``.
PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?P<name>\w+(?:\.\w+)*)\s*"
    r"(?:\|\s*truncate\s*(?:\(\s*(?P<limit>\d+)\s*\)\s*)?)?\}\}"
)


@dataclass(frozen=True)
class Placeholder:
    """One ``{{...}}`` occurrence in a template."""

    raw: str
    """The exact text to replace, filter included."""

    name: str
    """Dotted placeholder name, without the filter."""

    limit: Optional[int]
    """Byte cap from ``truncate``, or None when the filter is absent."""


def parse_placeholders(template: str) -> List[Placeholder]:
    """Every placeholder in ``template``, in order of appearance.

    Duplicates are returned as they occur; callers that resolve values
    deduplicate on :attr:`Placeholder.raw`, which is what a string replace
    operates on anyway.
    """
    if not template:
        return []
    placeholders: List[Placeholder] = []
    for match in PLACEHOLDER_RE.finditer(template):
        raw_limit = match.group("limit")
        if raw_limit is not None:
            limit: Optional[int] = int(raw_limit)
        elif "|" in match.group(0):
            limit = DEFAULT_TRUNCATE_BYTES
        else:
            limit = None
        placeholders.append(
            Placeholder(raw=match.group(0), name=match.group("name"), limit=limit)
        )
    return placeholders


def truncate_value(value: str, limit: Optional[int]) -> str:
    """Cap ``value`` at ``limit`` bytes, appending a marker when it is cut.

    Args:
        value: The resolved placeholder value.
        limit: Byte cap, or None to return ``value`` unchanged.

    Returns:
        ``value`` when it fits, otherwise its first ``limit`` bytes (cut at a
        UTF-8 boundary, never mid code point) followed by a marker naming the
        full size. The marker is part of the prompt on purpose: an agent that
        cannot tell a complete body from a prefix will answer questions about
        the missing part as if it had read it.
    """
    if limit is None or limit < 0:
        return value
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    # ``errors="ignore"`` drops a trailing partial code point rather than
    # emitting a replacement character.
    head = encoded[:limit].decode("utf-8", errors="ignore")
    return (
        f"{head}\n\n[truncated by Preloop: showing the first {limit} bytes of "
        f"{len(encoded)}; fetch the full text with the tool that owns this "
        f"object, for example get_pull_request]"
    )
