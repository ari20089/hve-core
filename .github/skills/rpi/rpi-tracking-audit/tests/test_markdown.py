# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Markdown parsing, vocabulary matching, and redaction helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from rpi_tracking_audit._markdown import (
    Doc,
    bullet_fields,
    capped,
    dash,
    field_table,
    labeled_blocks,
    line_number_reference,
    md_escape,
    plural,
    read_text,
    real_bullets,
    redact,
    redact_obj,
    short,
    table_blocks,
    vocab_match,
)
from rpi_tracking_audit._rules import OUTCOMES, PLAN_READINESS, VERDICTS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Complete and Ready", "Ready"),
        ("Not ready: research gap", "Not ready"),
        ("Blocked: none. Ready", "Ready"),
        ("{{Complete/Partial}}", None),
        ("", None),
    ],
)
def test_given_readiness_text_when_matched_then_vocabulary_entry(
    value: str, expected: str | None
) -> None:
    # Act
    result = vocab_match(value, PLAN_READINESS)

    # Assert
    assert result == expected


def test_given_negated_outcome_when_matched_then_next_entry_wins() -> None:
    # Act
    result = vocab_match("No defects found; Conformant", OUTCOMES)

    # Assert
    assert result == "Conformant"


def test_given_verdict_in_file_name_when_matched_with_strip_paths_then_ignored() -> None:
    # Act
    result = vocab_match("[link](reviews/revise-plan-critique.md) Pass", VERDICTS, strip_paths=True)

    # Assert
    assert result == "Pass"


def test_given_bold_labels_when_parsed_then_fields_read() -> None:
    # Act
    fields = bullet_fields("* **Status:** Partial\n- Scope: full plan\n* Status: ignored")

    # Assert
    assert fields == {"Status": "Partial", "Scope": "full plan"}


def test_given_table_without_outer_pipes_when_parsed_then_rows_read() -> None:
    # Act
    blocks = table_blocks("A | B\n--- | ---\n1 | 2\n\ntext")

    # Assert
    assert blocks == [(["A", "B"], [["1", "2"]])]


def test_given_field_table_with_bold_field_when_parsed_then_label_unwrapped() -> None:
    # Act
    table = field_table("| Field | Record |\n|---|---|\n| **Blockers** | none |")

    # Assert
    assert table == {"Blockers": "none"}


def test_given_doc_when_parsed_then_fenced_headings_ignored_and_sections_found() -> None:
    # Arrange
    doc = Doc("# T\n\n## One\n\n```\n## Not a heading\n```\n\n### Sub\n\nbody\n\n## Two\n")

    # Act
    subs = doc.subsection_bodies("One")

    # Assert
    assert doc.h2_titles() == ["One", "Two"]
    assert [t for t, _, _ in subs] == ["Sub"]
    assert doc.section("missing") == ""
    assert doc.heading_range(999) == (999, 1000)


def test_given_task_body_when_blocks_read_then_markers_end_blocks_and_fences_stay() -> None:
    # Arrange
    lines = [
        "Goals:",
        "* g <!-- note -->",
        "<!-- rpi:task id=P01-T01 -->",
        "* stray",
        "Details:",
        "* d",
        "```python",
        "# fixed columns",
        "```",
        "* FR-001",
        "#### Next",
        "* after",
    ]

    # Act
    blocks = labeled_blocks(lines)

    # Assert
    assert blocks == {
        "Goals": "* g  ",
        "Details": "* d\n```python\n# fixed columns\n```\n* FR-001",
    }


def test_given_private_key_when_redacted_then_line_count_kept() -> None:
    # Arrange
    text = "a\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\nb"

    # Act
    result = redact(text)

    # Assert
    assert "abc" not in result
    assert result.count("\n") == text.count("\n")


def test_given_nested_state_when_redacted_then_credential_keys_replaced() -> None:
    # Arrange
    obj = {"api_key": "x" * 10, "short_token": "abc", "list": [{"note": "AKIA" + "C" * 16}]}

    # Act
    result = redact_obj(obj)

    # Assert
    assert result["api_key"] == "[redacted value of api_key]"
    assert result["short_token"] == "abc"
    assert result["list"][0]["note"] == "[redacted AWS access key]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see src/app.py:42 for it", "src/app.py:42"),
        ("[x](a.md#L12)", "#L12"),
        ("on lines 3-5", "lines 3-5"),
        ("`src/app.py:42` only in code", None),
        ("x" * 2500 + " a.py:1", None),
    ],
)
def test_given_text_when_scanned_then_line_reference_found(text: str, expected: str | None) -> None:
    # Act
    result = line_number_reference(text)

    # Assert
    assert result == expected


def test_given_small_helpers_when_called_then_formatting_matches() -> None:
    # Assert
    assert plural(1, "task") == "1 task"
    assert plural(2, "entry", "entries") == "2 entries"
    assert capped(["a", "b", "c"], 2) == "a, b and 1 more"
    assert dash(None) == "-"
    assert md_escape("a|b\nc") == "a\\|b c"
    assert short("x" * 100, 10) == "x" * 9 + "…"
    assert real_bullets("* None\n* {{x}}\n- real\ntext") == ["real"]


def test_given_unreadable_path_when_read_then_empty(tmp_path: Path) -> None:
    # Act
    result = read_text(tmp_path / "missing.md")

    # Assert
    assert result == ""


def test_given_crlf_file_when_read_then_newlines_normalized(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "a.md"
    path.write_bytes(b"\xef\xbb\xbfa\r\nb\rc")

    # Act
    result = read_text(path)

    # Assert
    assert result == "a\nb\nc"
