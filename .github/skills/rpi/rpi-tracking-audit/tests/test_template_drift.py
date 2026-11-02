# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Fail when the RPI templates or the RPI Agent State contract drift from what the audit parses.

Runs only in a repository checkout, where the sibling rpi-* skills and the RPI Agent sit next
to this skill; distributed copies of the skill carry no tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rpi_tracking_audit._rules import REQUIRED_BY_KIND, STATE_CONTRACT_FIELDS

RPI = Path(__file__).resolve().parents[2]
REPO = RPI.parents[2]
TEMPLATES = {
    "plan": RPI / "rpi-plan" / "templates" / "implementation-plan.md",
    "critique": RPI / "rpi-plan-critique" / "templates" / "plan-critique.md",
    "changes": RPI / "rpi-implement" / "templates" / "changes-log.md",
    "review": RPI / "rpi-review" / "templates" / "review-log.md",
    "research": RPI / "rpi-research" / "templates" / "research.md",
}
LABELS = {
    "plan": [
        "Task ID:",
        "Task slug:",
        "| Planning execution and readiness",
        "| Latest critique",
        "Critique status:",
        "Latest critique and verdict:",
        "<!-- rpi:phase id=P01 -->",
        "<!-- rpi:task id=P01-T01 -->",
        "Changes in the map:",
    ],
    "critique": [
        "Critique execution: {{started_",
        "* Verdict:",
        "Critique type:",
        "<!-- rpi:critique id=PC-001 -->",
    ],
    "changes": [
        "* Status:",
        "* Declared invocation scope:",
        "* Completed scope markers:",
        "* All remaining active-plan markers:",
        "* Related plan:",
        "* Review readiness:",
        "Validation",
    ],
    "review": [
        "* Review execution:",
        "* Assessed outcome:",
        "* Final outcome:",
        "Final destination",
        "* Review scope:",
        "Proposed destination",
        "<!-- rpi:review id=RV-001 -->",
    ],
    "research": ["| Research disposition", "| Planning Readiness", "Evidence:"],
}


def _template(kind: str) -> str:
    path = TEMPLATES[kind]
    if not path.is_file():
        pytest.skip(f"{path} is not present outside a repository checkout")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", sorted(REQUIRED_BY_KIND))
def test_given_template_when_read_then_required_sections_exist(kind: str) -> None:
    # Arrange
    headings = {line[3:].strip() for line in _template(kind).splitlines() if line.startswith("## ")}

    # Act
    missing = [s for s in REQUIRED_BY_KIND[kind] if s not in headings]

    # Assert
    assert missing == []


@pytest.mark.parametrize("kind", sorted(LABELS))
def test_given_template_when_read_then_parsed_labels_exist(kind: str) -> None:
    # Arrange
    text = _template(kind)

    # Act
    missing = [label for label in LABELS[kind] if label not in text]

    # Assert
    assert missing == []


def test_given_state_contract_when_read_then_every_reported_field_is_defined() -> None:
    # Arrange
    agent = REPO / ".github" / "agents" / "hve-core" / "rpi-agent.agent.md"
    if not agent.is_file():
        pytest.skip("the RPI Agent is not present outside a repository checkout")
    text = agent.read_text(encoding="utf-8")

    # Act
    missing = [f for f in STATE_CONTRACT_FIELDS if f"`{f}`" not in text]

    # Assert
    assert missing == []
    assert ".copilot-tracking/rpi-sessions/{{YYYY-MM-DD}}/{{session_slug}}/" in text
