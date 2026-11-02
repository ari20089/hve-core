# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Markdown parsing, redaction, and small text helpers for the RPI tracking audit."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ._rules import (
    BOLD_WRAP,
    BULLET_FIELD,
    CHANGES_IN_MAP,
    HEADING,
    LABEL_BLOCK,
    LINE_REF_ANCHOR,
    LINE_REF_PHRASE,
    LINE_REF_TOKEN,
    LIST_CAP,
    LONG_LINE,
    MARKER,
    MD_LINK,
    PLACEHOLDER,
    PRIVATE_KEY_BODY,
    SECRET_KEY,
    SECRET_PATTERNS,
    TABLE_SEP,
)


def read_text_checked(path: Path) -> tuple[str, str | None]:
    """The file as UTF-8 with normalized newlines and no error, or '' and the read error."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return "", exc.strerror or type(exc).__name__
    return text.replace("\r\n", "\n").replace("\r", "\n"), None


def read_text(path: Path) -> str:
    """Read a file as UTF-8 with normalized newlines, or return '' when it cannot be read."""
    return read_text_checked(path)[0]


def md_escape(text: object) -> str:
    """Escape a value for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def plural(count: int, noun: str, plural_noun: str | None = None) -> str:
    """Format a count with the singular or plural noun."""
    return f"{count} {noun if count == 1 else (plural_noun or noun + 's')}"


def capped(items: Iterable[str], cap: int = LIST_CAP, sep: str = ", ") -> str:
    """Join items, listing at most `cap` and counting the rest."""
    items = list(items)
    if len(items) <= cap:
        return sep.join(items)
    return sep.join(items[:cap]) + f" and {len(items) - cap} more"


def redact(text: str) -> str:
    """Replace credential-shaped values with a labeled placeholder.

    The line count is preserved so line numbers stay valid.
    """
    text = PRIVATE_KEY_BODY.sub(
        lambda mm: "[redacted private key block]" + "\n" * mm.group(0).count("\n"), text
    )
    for label, pat in SECRET_PATTERNS:
        text = pat.sub(f"[redacted {label}]", text)
    return text


def redact_obj(obj: Any) -> Any:
    """Apply redact() to every string in nested dicts and lists.

    A string of eight or more characters under a key named like a credential is
    replaced whole.
    """
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = obj.__class__()
        for key, value in obj.items():
            key = redact(key) if isinstance(key, str) else key
            if (
                isinstance(key, str)
                and isinstance(value, str)
                and len(value) >= 8
                and SECRET_KEY.search(key)
            ):
                out[key] = f"[redacted value of {key}]"
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def short(text: object, cap: int = 90) -> str:
    """Collapse whitespace, redact, and truncate to `cap` characters."""
    value = redact(" ".join(str(text).split()))
    return value if len(value) <= cap else value[: cap - 1] + "…"


def dash(value: object) -> str:
    """Render an empty value as '-'."""
    return "-" if value in (None, "", "None") else str(value)


def parse_heading(line: str) -> tuple[int, str] | None:
    """(level, text) of an ATX heading line, or None.

    Lines over LONG_LINE are never headings; closing hashes are stripped in code so the
    pattern stays linear.
    """
    if len(line) > LONG_LINE:
        return None
    m = HEADING.match(line)
    if not m:
        return None
    text = m.group(2).rstrip()
    unclosed = text.rstrip("#")
    if unclosed != text and (not unclosed or unclosed[-1] in " \t"):
        text = unclosed.rstrip()
    return (len(m.group(1)), text)


class Doc:
    """A parsed tracking Markdown file: lines, headings, markers, and H2 sections."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.split("\n")
        self.headings: list[tuple[int, str, int]] = []  # (level, text, line_index)
        self.markers: list[tuple[str, str, int]] = []  # (kind, id, line_index)
        in_fence = False
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence or len(line) > LONG_LINE:
                continue
            heading = parse_heading(line)
            if heading:
                self.headings.append((heading[0], heading[1], i))
            for mm in MARKER.finditer(line):
                self.markers.append((mm.group(1), mm.group(2), i))
        self.sections: dict[str, tuple[int, int]] = {}  # H2 title -> (start, end)
        h2 = [(t, i) for lvl, t, i in self.headings if lvl == 2]
        for idx, (title, start) in enumerate(h2):
            end = h2[idx + 1][1] if idx + 1 < len(h2) else len(self.lines)
            self.sections.setdefault(title, (start, end))
        # Body ends, precomputed so lookups stay linear in the number of headings.
        self._range_end: dict[int, int] = {}
        open_headings: list[tuple[int, int]] = []
        for lvl, _, i in self.headings:
            while open_headings and open_headings[-1][0] >= lvl:
                self._range_end[open_headings.pop()[1]] = i
            open_headings.append((lvl, i))
        for _, i in open_headings:
            self._range_end[i] = len(self.lines)
        starts = [i for _, _, i in self.headings]
        # zip stops at the shorter list, so a file without headings maps nothing
        self._next_heading: dict[int, int] = dict(zip(starts, [*starts[1:], len(self.lines)]))

    def section(self, name: str) -> str:
        """Text of the H2 section whose title starts with `name` (case-insensitive), or ''."""
        for title, (start, end) in self.sections.items():
            if title.lower().startswith(name.lower()):
                return "\n".join(self.lines[start + 1 : end])
        return ""

    def has_section(self, name: str) -> bool:
        """Whether an H2 section title starts with `name`."""
        return any(t.lower().startswith(name.lower()) for t in self.sections)

    def h2_titles(self) -> list[str]:
        """The H2 titles in order."""
        return list(self.sections.keys())

    def heading_range(self, line_index: int) -> tuple[int, int]:
        """The heading's body: until the next heading of equal or higher level."""
        return (line_index, self._range_end.get(line_index, line_index + 1))

    def body_lines(self, line_index: int) -> list[str]:
        """Lines under the heading at `line_index`, nested headings included."""
        start, end = self.heading_range(line_index)
        return self.lines[start + 1 : end]

    def own_body_lines(self, line_index: int) -> list[str]:
        """Lines under a heading up to the next heading of any level, outside code fences."""
        return self.lines[line_index + 1 : self._next_heading.get(line_index, line_index + 1)]

    def subsection_bodies(self, section_name: str, level: int = 3) -> list[tuple[str, str, int]]:
        """(title, body_text, line) for each heading of `level` inside the named H2 section."""
        out = []
        for title, (start, end) in self.sections.items():
            if not title.lower().startswith(section_name.lower()):
                continue
            for lvl, t, i in self.headings:
                if lvl == level and start < i < end:
                    out.append((t, "\n".join(self.body_lines(i)), i))
            break
        return out


def bullet_fields(text: str) -> dict[str, str]:
    """`* Label: value` bullets (bold labels allowed) as label -> value; first occurrence wins."""
    out: dict[str, str] = {}
    for line in text.split("\n"):
        m = BULLET_FIELD.match(line[:LONG_LINE])
        if not m:
            continue
        label = BOLD_WRAP.sub("", m.group(1)).strip()
        value = m.group(2).strip()
        if value.startswith(
            ("**", "__")
        ):  # `* **Status:** Partial` closes the bold after the colon
            value = value[2:].strip()
        if label and label not in out:
            out[label] = value
    return out


def _split_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def table_blocks(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Pipe tables in text as (header_cells, rows) pairs, separator rows dropped.

    Accepts tables with or without outer pipes; a block counts as a table when its
    second line is a separator row.
    """
    blocks: list[tuple[list[str], list[list[str]]]] = []
    block: list[str] = []

    def flush() -> None:
        if len(block) >= 2 and all(TABLE_SEP.match(c or "-") for c in _split_cells(block[1])):
            blocks.append((_split_cells(block[0]), [_split_cells(line) for line in block[2:]]))
        block.clear()

    for line in text.split("\n"):
        if "|" in line and not line.strip().startswith(("```", "~~~")):
            block.append(line)
        else:
            flush()
    flush()
    return blocks


def table_rows(text: str) -> list[list[str]]:
    """Rows of pipe tables in text, header and separator rows dropped."""
    return [row for _, rows in table_blocks(text) for row in rows]


def field_table(text: str) -> dict[str, str]:
    """A two-column `| Field | Record |` table as a dict."""
    out: dict[str, str] = {}
    for cells in table_rows(text):
        if len(cells) >= 2 and cells[0] and cells[0] not in out:
            out[BOLD_WRAP.sub("", cells[0]).strip()] = cells[1]
    return out


def labeled_blocks(body_lines: list[str]) -> dict[str, str]:
    """Labeled blocks (Goals:, Requirements:, ...) inside a task or phase body.

    Stops at the first heading outside a code fence so a phase does not absorb its tasks'
    blocks; marker comments end a block, and a fenced block stays in the block it follows.
    A phase's `Changes in the map:` line is kept as its own one-line block.
    """
    blocks: dict[str, list[str]] = {}
    current = None
    in_fence = False
    for line in body_lines:
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif in_fence:
            pass
        elif parse_heading(line):
            break
        elif MARKER.search(line):
            current = None
            continue
        elif CHANGES_IN_MAP.match(line.strip()):
            blocks["Changes in the map"] = [line.strip().split(":", 1)[1].strip()]
            current = None
            continue
        elif m := LABEL_BLOCK.match(line.strip()):
            current = m.group(1)
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(re.sub(r"<!--.*?-->", " ", line))
    return {k: "\n".join(v) for k, v in blocks.items()}


def strip_fences_and_comments(text: str) -> str:
    """Remove HTML comments and fenced code blocks."""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.strip().startswith("```") or line.strip().startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def strip_inline_code(text: str) -> str:
    """Remove inline code spans."""
    return re.sub(r"`[^`\n]*`", " ", text)


def is_placeholder_value(value: str | None) -> bool:
    """Whether a value is empty or still holds a `{{placeholder}}`."""
    v = (value or "").strip()
    return not v or PLACEHOLDER.search(v) is not None


def vocab_match(
    value: str | None, vocabulary: Iterable[str], strip_paths: bool = False
) -> str | None:
    """The vocabulary entry that occurs earliest in value as a whole word, or None.

    The longest entry wins a tie. Placeholders count as absent. With strip_paths,
    Markdown links and path-like tokens are removed first so a verdict is not read out of
    a file name such as `revise-login-plan-critique.md`. A negated entry ("No defects
    found") and a labeled-none entry ("Blocked: none") are skipped.
    """
    vocabulary = list(vocabulary)
    v = (value or "")[:LONG_LINE]
    if is_placeholder_value(v):
        return None
    if strip_paths:
        v = MD_LINK.sub(" ", v)
        v = re.sub(r"\S*/\S*|\S+\.md\b", " ", v)
    alternatives = "|".join(re.escape(e) for e in sorted(vocabulary, key=len, reverse=True))
    pattern = r"(?<![\w-])(" + alternatives + r")(?![\w-])"
    for m in re.finditer(pattern, v, re.I):
        if re.search(r"\bno\s*$", v[: m.start()], re.I):
            continue
        if re.match(r"\s*:\s*none\b", v[m.end() :], re.I):
            continue
        low = m.group(1).lower()
        for entry in vocabulary:
            if entry.lower() == low:
                return entry
    return None


def real_bullets(text: str) -> list[str]:
    """Top-level bullets of a section that are neither `None` nor unfilled placeholders."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith(("* ", "- ")):
            continue
        item = s[2:].strip()
        if not item or re.match(r"^none\b", item, re.I) or is_placeholder_value(item):
            continue
        out.append(item)
    return out


def line_number_reference(text: str) -> str | None:
    """The first line-number reference outside code, or None.

    Token-based so very long lines stay linear.
    """
    for line in strip_inline_code(text).split("\n"):
        if len(line) > LONG_LINE:
            continue
        m = LINE_REF_ANCHOR.search(line) or LINE_REF_PHRASE.search(line)
        if m:
            return m.group(0)
        for tok in line.split():
            if LINE_REF_TOKEN.match(tok):
                return tok
    return None
