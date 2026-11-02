# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Command-line interface for the RPI tracking audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from ._audit import Audit, UsageError
from ._markdown import plural, redact
from ._render import render_markdown, to_json

VERSION = "0.0.0"
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2


def create_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        prog="rpi_tracking_audit",
        description="Audit the RPI tracking artifacts under .copilot-tracking/ against the "
        "hve-core RPI conventions. Read-only: the audit changes no file it reads.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path("."),
        help="repository or workspace root (default: current directory)",
    )
    parser.add_argument("--md", type=Path, help="write the Markdown report to this file")
    parser.add_argument("--json", type=Path, help="write the JSON report to this file")
    parser.add_argument("--brief", action="store_true", help="chat-sized Markdown report")
    parser.add_argument("--task", help="report one task slug only")
    parser.add_argument("--file", type=Path, help="audit one tracking artifact alone")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=30,
        help="days without a change after which an open task is stale (0 disables)",
    )
    parser.add_argument("--as-of", help="reference date YYYY-MM-DD (default: today)")
    parser.add_argument("--max-rows", type=int, default=25, help="rows per table before truncation")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def _validate(args: argparse.Namespace) -> date:
    try:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    except ValueError as exc:
        raise UsageError("--as-of expects YYYY-MM-DD") from exc
    if args.max_rows < 1:
        raise UsageError("--max-rows must be at least 1")
    if args.stale_days < 0:
        raise UsageError("--stale-days must be 0 or more")
    if args.file and args.task:
        raise UsageError("--task cannot be combined with --file")
    return as_of


def run(args: argparse.Namespace) -> int:
    """Run the audit and write the requested reports."""
    as_of = _validate(args)
    single_path = None
    if args.file:
        if not args.file.is_file():
            raise UsageError(f"not a file: {args.file}")
        path = Path(os.path.normpath(args.file.absolute()))
        audit = Audit(path.parent, as_of, 0, single=True)
        audit.run_single(path)
        single_path = str(args.file)
    else:
        root = Path(os.path.normpath(args.root.absolute()))
        if not root.is_dir():
            raise UsageError(f"not a folder: {root}")
        audit = Audit(root, as_of, args.stale_days)
        audit.run_workspace()
        if args.task:
            audit.filter_task(args.task)
    audit.finalize()

    md = redact(
        render_markdown(
            audit, VERSION, brief=args.brief, max_rows=args.max_rows, single_path=single_path
        )
    )
    if args.md:
        args.md.write_text(md, encoding="utf-8")
    if args.json:
        payload = json.dumps(to_json(audit, VERSION, single_path), indent=2, default=str)
        args.json.write_text(payload, encoding="utf-8")
    if not args.md and not args.json:
        sys.stdout.write(md)
        return EXIT_SUCCESS
    sev = Counter(f.severity for f in audit.findings)
    legacy = f", {plural(len(audit.legacy_tasks), 'legacy task')}" if audit.legacy_tasks else ""
    written = " and ".join(str(p) for p in (args.md, args.json) if p)
    sys.stdout.write(
        f"{plural(len(audit.findings), 'finding')}, {sev['high']} high, {sev['medium']} medium; "
        f"{plural(len(audit.tasks), 'task')}{legacy}. Wrote {written}\n"
    )
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    """Main entry point with error handling."""
    args = create_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # A console or pipe that cannot encode a character gets a replacement, not a crash.
        sys.stdout.reconfigure(errors="replace")
    try:
        return run(args)
    except UsageError as exc:
        print(f"Error: {redact(str(exc))}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except BrokenPipeError:
        sys.stderr.close()
        return EXIT_FAILURE
    except OSError as exc:
        print(f"Error: {redact(str(exc))}", file=sys.stderr)
        return EXIT_FAILURE
