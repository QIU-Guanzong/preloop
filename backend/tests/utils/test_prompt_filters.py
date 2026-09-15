"""The prompt template's ``truncate`` filter bounds what a webhook injects."""

from __future__ import annotations

from preloop.utils.prompt_filters import (
    DEFAULT_TRUNCATE_BYTES,
    Placeholder,
    parse_placeholders,
    truncate_value,
)


class TestParsing:
    def test_a_plain_placeholder_still_parses_as_before(self):
        assert parse_placeholders("x {{project.name}} y") == [
            Placeholder(raw="{{project.name}}", name="project.name", limit=None)
        ]

    def test_an_explicit_cap_is_read_off_the_filter(self):
        (item,) = parse_placeholders("{{a.b|truncate(1024)}}")
        assert (item.name, item.limit) == ("a.b", 1024)

    def test_a_bare_filter_uses_the_default_cap(self):
        (item,) = parse_placeholders("{{a.b|truncate}}")
        assert item.limit == DEFAULT_TRUNCATE_BYTES

    def test_whitespace_around_the_filter_is_tolerated(self):
        (item,) = parse_placeholders("{{ a.b | truncate(10) }}")
        assert (item.raw, item.name, item.limit) == (
            "{{ a.b | truncate(10) }}",
            "a.b",
            10,
        )

    def test_the_raw_text_is_what_a_caller_must_replace(self):
        # The orchestrator replaces item.raw, not "{{" + name + "}}": with a
        # filter those two differ, and replacing the wrong one leaves the
        # placeholder in the prompt for the model to read literally.
        template = "{{a.b|truncate(4)}}"
        (item,) = parse_placeholders(template)
        assert template.replace(item.raw, "VALUE") == "VALUE"

    def test_an_empty_template_has_no_placeholders(self):
        assert parse_placeholders("") == []


class TestTruncation:
    def test_a_value_that_fits_is_returned_untouched(self):
        assert truncate_value("short", 1024) == "short"

    def test_no_limit_means_no_change(self):
        assert truncate_value("x" * 10_000, None) == "x" * 10_000

    def test_an_oversized_value_is_cut_and_says_so(self):
        value = "y" * 100
        out = truncate_value(value, 10)
        assert out.startswith("y" * 10)
        assert "showing the first 10 bytes of 100" in out
        # The agent must be able to go get the rest.
        assert "get_pull_request" in out

    def test_the_cut_never_splits_a_code_point(self):
        # A byte cap landing mid-character must not emit a replacement
        # character or raise; it drops the partial code point.
        value = "é" * 10  # two bytes each
        out = truncate_value(value, 5)
        assert out.startswith("é" * 2)
        assert "�" not in out

    def test_the_cap_counts_bytes_not_characters(self):
        # A character count would let a non-ASCII body through at twice the
        # intended size, which is the size that matters downstream.
        value = "é" * 100  # 200 bytes
        assert truncate_value(value, 300) == value
        assert len(truncate_value(value, 100).encode()) > 100  # marker included
        assert truncate_value(value, 100).split("\n\n")[0] == "é" * 50
