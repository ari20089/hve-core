# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Find and classify the files under .copilot-tracking/ without following symbolic links."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ._markdown import Doc, is_placeholder_value, read_text, read_text_checked, redact, redact_obj
from ._rules import (
    AREAS,
    DATE_DIR,
    KNOWN_OTHER_AREAS,
    LEGACY_AREAS,
    LEGACY_DISTINCTIVE,
    LEGACY_HINTS,
    LEGACY_PLAN,
    MAX_SCAN_DEPTH,
    MAX_STATE_BYTES,
    MD_LINK,
    SKIP_DIRS,
    STATE_FIELDS,
    TRACKING_DIR,
)


def relpath(path: Path | str, root: Path | str) -> str:
    """POSIX-style path of `path` relative to `root`.

    Uses os.path.relpath because a resolved link target may lie outside the root, which
    Path.relative_to rejects.
    """
    return Path(os.path.relpath(path, root)).as_posix()


def workspace_file(root: Path, target: Path | str) -> bool:
    """Whether target is a regular file inside root, reached without a symbolic link.

    Paths named inside artifacts are untrusted: a path that leaves the workspace or passes
    through a link is never probed, and an over-long or malformed path counts as missing.
    """
    try:
        # os.path.normpath: pathlib has no lexical normalization of `..` segments
        full = Path(os.path.normpath(root / target))
        if not full.is_relative_to(root):
            return False
        current = root
        for part in full.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                return False
        return current.is_file()
    except (OSError, ValueError):
        return False


def inside_workspace(root: Path, target: Path | str) -> bool:
    """Whether target stays inside root after lexical normalization."""
    try:
        return Path(os.path.normpath(root / target)).is_relative_to(root)
    except ValueError:
        return False


def file_mtime(path: Path) -> date | None:
    """The file's modification date, or None."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


class Artifact:
    """One tracking Markdown file with its parsed document and derived model."""

    def __init__(
        self,
        root: Path,
        path: Path,
        area: str,
        kind: str,
        slug: str,
        date_dir: str | None,
        suffix_n: int | None,
    ) -> None:
        self.root = root
        self.path = path
        self.rel = relpath(path, root)
        self.area = area
        self.kind = kind
        self.slug = slug
        self.date_dir = date_dir
        self.suffix_n = suffix_n
        self.raw_text, self.read_error = read_text_checked(path)
        # every parsed field derives from the redacted text; HY1 scans raw_text
        self.text = redact(self.raw_text)
        self.doc = Doc(self.text)
        self.mtime = file_mtime(path)
        self.model: dict[str, Any] = {}
        self.legacy: str | None = None  # subkind when the file is in the retired format
        self.unrecognized = False

    def sort_key(self) -> tuple[str, int, str]:
        """Order by date folder, then follow-up number, then name."""
        return (self.date_dir or "", self.suffix_n or 0, self.path.name)


@dataclass
class TrackingRoots:
    """Where tracking folders were found."""

    top: Path | None
    nested: list[str]
    depth_limited: bool
    top_is_link: bool


@dataclass
class StateFile:
    """An RPI Agent session state file recognized by its fields."""

    path: Path
    rel: str
    state: dict[str, Any]
    raw_text: str


@dataclass
class Discovery:
    """Everything found under the tracking folder."""

    artifacts: list[Artifact] = field(default_factory=list)
    legacy: list[Artifact] = field(default_factory=list)
    misnamed: list[tuple[str, str, str, str]] = field(default_factory=list)  # rel, area, kind, slug
    extras: list[tuple[str, str, str]] = field(default_factory=list)  # rel, area, slug
    state_files: list[StateFile] = field(default_factory=list)
    other_areas: Counter[str] = field(default_factory=Counter)
    symlinks: list[str] = field(default_factory=list)


def find_tracking_roots(root: Path) -> TrackingRoots:
    """The root tracking folder, nested tracking folders, and scan limits.

    A root tracking folder that is a symbolic link is reported and never followed.
    """
    top = root / TRACKING_DIR
    top_is_link = top.is_symlink()
    top_dir = top if top.is_dir() and not top_is_link else None
    nested: list[str] = []
    depth_limited = False
    for dirpath, dirnames, _ in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (here / d).is_symlink()]
        depth = 0 if here == root else relpath(here, root).count("/") + 1
        if depth >= MAX_SCAN_DEPTH:
            if dirnames:
                depth_limited = True
            dirnames[:] = []
            continue
        for d in list(dirnames):
            if d == TRACKING_DIR:
                full = here / d
                if full.absolute() != top.absolute():
                    nested.append(relpath(full, root))
                dirnames.remove(d)
    return TrackingRoots(top_dir, sorted(nested), depth_limited, top_is_link)


def classify_file(rel_in_tracking: str) -> tuple[Any, ...] | None:
    """Classify a path relative to .copilot-tracking/.

    Returns ('current', area, kind, slug, date_dir, n, date_ok, name_ok),
    ('legacy', area, subkind, slug, date_dir), or None outside the RPI areas.
    """
    parts = rel_in_tracking.split("/")
    fn = parts[-1]
    # legacy areas first: they nest inside current areas (plans/logs, research/subagents)
    for area_name, (subkind, pattern) in LEGACY_AREAS.items():
        area_parts = area_name.split("/")
        if parts[: len(area_parts)] == area_parts and len(parts) >= len(area_parts) + 1:
            remainder = parts[len(area_parts) :]
            if area_name == "reviews" and remainder[0] in (
                "plans",
                "logs",
                "code-reviews",
                "architecture",
                "rpi",
            ):
                break
            if area_name == "reviews" and not (
                len(remainder) == 2 and DATE_DIR.match(remainder[0])
            ):
                break
            m = pattern.match(fn)
            slug = m.group("slug") if m else re.sub(r"\.md$", "", fn)
            return (
                "legacy",
                area_name,
                subkind,
                slug,
                remainder[0] if len(remainder) >= 2 else None,
            )
    legacy_plan = LEGACY_PLAN.match(fn)
    if parts[0] == "plans" and legacy_plan:
        date_dir = parts[1] if len(parts) >= 3 else None
        return ("legacy", "plans", "implementation plan", legacy_plan.group("slug"), date_dir)
    for area_name, (kind, pattern) in AREAS.items():
        area_parts = area_name.split("/")
        if parts[: len(area_parts)] == area_parts and len(parts) >= len(area_parts) + 1:
            remainder = parts[len(area_parts) :]
            date_dir = next((p for p in remainder[:-1] if DATE_DIR.match(p)), None)
            m = pattern.match(fn)
            slug = m.group("slug") if m else re.sub(r"\.md$", "", fn)
            n = int(m.group("n")) if (m and m.groupdict().get("n")) else None
            date_ok = len(remainder) == 2 and DATE_DIR.match(remainder[0]) is not None
            return ("current", area_name, kind, slug, date_dir, n, date_ok, m is not None)
    return None


def parse_state(text: str) -> dict[str, Any] | None:
    """A redacted RPI Agent state object, or None when the JSON is not one."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if isinstance(data, dict) and STATE_FIELDS.issubset(data.keys()):
        return redact_obj(data)
    return None


def load_state(path: Path) -> tuple[dict[str, Any], str] | None:
    """The redacted state and the raw text it was parsed from, or None.

    Files over MAX_STATE_BYTES are not parsed.
    """
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
    except OSError:
        return None
    raw = read_text(path)
    state = parse_state(raw)
    return (state, raw) if state is not None else None


def looks_legacy(art: Artifact, legacy_slugs: set[str] | frozenset[str] = frozenset()) -> bool:
    """A research or changes file at a current path but written in the retired format.

    True when the file has none of the sections only the current skills write and
    either carries a legacy hint or belongs to a slug with legacy siblings.
    """
    if art.kind not in ("research", "changes") or not art.text.strip():
        return False
    if any(art.doc.has_section(s) for s in LEGACY_DISTINCTIVE[art.kind]):
        return False
    if any(h in art.text for h in LEGACY_HINTS):
        return True
    first = art.doc.headings[0][1] if art.doc.headings else ""
    current_header = first.startswith(("Task Research:", "RPI Changes:"))
    return art.slug in legacy_slugs and not current_header


def discover(root: Path, tracking_root: Path) -> Discovery:
    """Walk the tracking folder and sort every file into its bucket."""
    found = Discovery()
    links: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(tracking_root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if (here / d).is_symlink():
                links.add(relpath(here / d, root))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = here / fn
            if full.is_symlink():
                links.add(relpath(full, root))
                continue
            if not full.is_file():
                # a FIFO or device would block or fail on read
                found.other_areas["(special files, not read)"] += 1
                continue
            rel_t = relpath(full, tracking_root)
            if fn.lower().endswith(".json"):
                loaded = load_state(full)
                if loaded is not None:
                    found.state_files.append(StateFile(full, relpath(full, root), *loaded))
                    continue
            info = classify_file(rel_t)
            if info is None:
                segs = rel_t.split("/")
                if len(segs) == 1:
                    found.other_areas["(files at the tracking root)"] += 1
                else:
                    two = "/".join(segs[:2])
                    found.other_areas[two if two in KNOWN_OTHER_AREAS else segs[0]] += 1
                continue
            if info[0] == "legacy":
                _, area, subkind, slug, date_dir = info
                art = Artifact(root, full, area, "legacy", slug, date_dir, None)
                art.legacy = subkind
                found.legacy.append(art)
                continue
            _, area, kind, slug, date_dir, n, date_ok, name_ok = info
            if not fn.lower().endswith(".md"):
                found.extras.append((relpath(full, root), area, slug))
                continue
            if not name_ok:
                found.misnamed.append((relpath(full, root), area, kind, slug))
                continue
            art = Artifact(root, full, area, kind, slug, date_dir, n)
            art.model["date_ok"] = date_ok
            found.artifacts.append(art)
    # second pass: research and changes files in the retired format
    legacy_slugs = {a.slug for a in found.legacy}
    current = []
    for art in found.artifacts:
        if looks_legacy(art, legacy_slugs):
            art.legacy = {"research": "research document", "changes": "changes log"}[art.kind]
            art.kind = "legacy"
            found.legacy.append(art)
        else:
            current.append(art)
    found.artifacts = current
    found.symlinks = sorted(links)
    return found


def resolve_ref(raw: str | None, art_dir: Path, root: Path) -> tuple[str, bool, str | None] | None:
    """Resolve a path mention (Markdown link, plain, or backticked) to a file.

    Returns (display, exists, resolved_rel), or None when the value names no path.
    """
    raw = (raw or "").strip()
    if not raw or is_placeholder_value(raw):
        return None
    m = MD_LINK.search(raw)
    candidates: list[Path] = []
    if m:
        text = m.group(1).strip()
        dest = unquote(m.group(2).split("#")[0])
        display = dest
        generic = ("link", "plan", "changes", "research", "critique", "review")
        if text and text != dest and text.lower() not in generic:
            display = f"{dest} (link text {text})"
        candidates.append(art_dir / dest)
        if dest.startswith(TRACKING_DIR):
            candidates.append(root / dest)
    else:
        display = raw.strip("`").strip()
        if not display or display.lower() in ("none", "not applicable", "n/a", "skipped"):
            return None
        display = display.split(" ")[0].strip("`,;")
        if not re.search(r"\.(md|ya?ml|json)$", display):
            return None
        candidates.append(root / display)
        candidates.append(art_dir / display)
    for cand in candidates:
        if workspace_file(root, cand):
            return (display, True, relpath(os.path.normpath(cand), root))
    if not any(inside_workspace(root, cand) for cand in candidates):
        display += " (outside the workspace, not checked)"
    return (display, False, None)
