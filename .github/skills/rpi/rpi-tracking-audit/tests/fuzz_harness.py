# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Polyglot fuzz harness for the RPI tracking audit parsers.

Runs as a pytest test when Atheris is not installed.
Runs as an Atheris coverage-guided fuzz target when executed directly.
The first byte of each file in tests/corpus/ selects the target: 0 models,
1 vocabulary, 2 state redaction, 3 paths and tables.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from rpi_tracking_audit._discovery import classify_file
from rpi_tracking_audit._markdown import (
    Doc,
    redact,
    redact_obj,
    table_blocks,
    vocab_match,
)
from rpi_tracking_audit._models import (
    model_changes,
    model_critique,
    model_legacy,
    model_plan,
    model_research,
    model_review,
)
from rpi_tracking_audit._rules import OUTCOMES, PLAN_READINESS, VERDICTS

try:
    import atheris
except ImportError:
    atheris = None
    FUZZING = False
else:
    FUZZING = True

MODELS = (model_plan, model_critique, model_changes, model_review, model_research, model_legacy)


def parse_all_models(text: str) -> None:
    """Parse one Markdown text as every artifact kind."""
    redacted = redact(text)
    for model in MODELS:
        art = SimpleNamespace(doc=Doc(redacted), model={}, text=redacted, raw_text=text)
        model(art)


def fuzz_models(data: bytes) -> None:
    """Fuzz the Markdown document parser and every artifact model."""
    provider = atheris.FuzzedDataProvider(data)
    parse_all_models(provider.ConsumeUnicodeNoSurrogates(provider.remaining_bytes()))


def fuzz_vocab(data: bytes) -> None:
    """Fuzz vocabulary matching, with and without path stripping."""
    provider = atheris.FuzzedDataProvider(data)
    value = provider.ConsumeUnicodeNoSurrogates(provider.remaining_bytes())
    vocab_match(value, PLAN_READINESS)
    vocab_match(value, OUTCOMES)
    vocab_match(value, VERDICTS, strip_paths=True)


def fuzz_state_redaction(data: bytes) -> None:
    """Fuzz redaction of arbitrary JSON values and text."""
    provider = atheris.FuzzedDataProvider(data)
    text = provider.ConsumeUnicodeNoSurrogates(provider.remaining_bytes())
    redact(text)
    try:
        redact_obj(json.loads(text))
    except (ValueError, RecursionError):
        pass


def fuzz_paths_and_tables(data: bytes) -> None:
    """Fuzz tracking-path classification and pipe-table parsing."""
    provider = atheris.FuzzedDataProvider(data)
    text = provider.ConsumeUnicodeNoSurrogates(provider.remaining_bytes())
    classify_file(text.replace("\\", "/").lstrip("/") or "x")
    table_blocks(text)


FUZZ_TARGETS = [
    fuzz_models,
    fuzz_vocab,
    fuzz_state_redaction,
    fuzz_paths_and_tables,
]


def fuzz_dispatch(data: bytes) -> None:
    """Route input to one fuzz target."""
    if len(data) < 2:
        return
    target_index = data[0] % len(FUZZ_TARGETS)
    FUZZ_TARGETS[target_index](data[1:])


class TestRpiTrackingAuditFuzzHarness:
    """Property tests mirroring the fuzz targets (run under pytest)."""

    def test_models_never_raise_on_malformed_markdown(self) -> None:
        samples = (
            "",
            "#",
            "<!-- rpi:task id=P01-T01 -->",
            "## Phase Checklist\n#### [x] P01-T01\n| a |\n|---|\n",
            "## Parent Decision Record\n| RD-001 | RV-001 |\n|-|-|\n| RD-001 | RV-001 | x |",
            "```\n## Findings\n",
        )
        for text in samples:
            parse_all_models(text)

    def test_vocab_match_returns_entry_or_none(self) -> None:
        for value in ("", "Ready", "No Ready", "{{x}}", "a/b.md Pass"):
            assert vocab_match(value, PLAN_READINESS + VERDICTS) in (
                None,
                *PLAN_READINESS,
                *VERDICTS,
            )

    def test_redact_obj_handles_any_json(self) -> None:
        for value in ([], {"token": "x" * 9}, [1, None, "AKIA" + "D" * 16], 3.5):
            redact_obj(value)

    def test_classify_file_handles_any_path(self) -> None:
        for rel in ("x", "plans", "reviews/2026-01-01/a-review.md", "reviews/logs/x.md"):
            classify_file(rel)


if __name__ == "__main__" and FUZZING:
    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_dispatch)
    atheris.Fuzz()
