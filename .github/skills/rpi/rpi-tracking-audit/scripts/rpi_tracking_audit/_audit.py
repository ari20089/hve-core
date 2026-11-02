# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Evaluate the rules, group artifacts into tasks, and derive each task's state and next action."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from ._discovery import (
    Artifact,
    StateFile,
    discover,
    find_tracking_roots,
    looks_legacy,
    relpath,
    resolve_ref,
    workspace_file,
)
from ._markdown import (
    capped,
    is_placeholder_value,
    line_number_reference,
    plural,
    read_text,
    short,
    strip_fences_and_comments,
)
from ._models import (
    model_changes,
    model_critique,
    model_legacy,
    model_plan,
    model_research,
    model_review,
)
from ._rules import (
    AREAS,
    CROSS_ARTIFACT_RULES,
    DATE_DIR,
    DESTINATIONS,
    FILE_DIRECTIVE,
    KEBAB,
    KIND_LABEL,
    KIND_ORDER,
    LEGACY_PLAN,
    PLACEHOLDER,
    REQUIRED_BY_KIND,
    RULES,
    SECRET_PATTERNS,
    SESSIONS_AREA,
    SEVERITY_ORDER,
    SLUG_MAX,
    STARTED,
    STATE_FILE,
    STATE_JOIN_ORDER,
    STATE_RULES,
    SUFFIX_BY_KIND,
    TRACKING_DIR,
)

Task = dict[str, list[Artifact]]
GITIGNORE_LINE = re.compile(r"^\s*(?:\*\*/)?/?\.copilot-tracking(?:/(?:\*\*?)?)?\s*(#.*)?$")
LEGACY_KIND_BY_KIND = {"research": "research document", "changes": "changes log"}
# decisions that leave a material finding open (rpi-review forbids Conformant over them)
MATERIAL_OPEN = re.compile(r"\b(?:deferred|skipped)\b", re.I)


def review_step_skipped(state: dict[str, Any]) -> bool:
    """Whether the latest `Review step` decision in confirmed_decisions is `skip`."""
    decisions = state.get("confirmed_decisions")
    skipped = False
    for d in decisions if isinstance(decisions, list) else []:
        if isinstance(d, dict) and str(d.get("decision") or "").strip().lower() == "review step":
            skipped = re.search(r"\bskip", str(d.get("status") or ""), re.I) is not None
    return skipped


class UsageError(Exception):
    """A request the audit cannot run; reported with exit code 2."""


class Finding:
    """One rule hit on one artifact."""

    def __init__(
        self,
        rule: str,
        artifact_rel: str,
        detail: str,
        slug: str | None = None,
        severity: str | None = None,
    ) -> None:
        self.rule = rule
        self.severity = severity or RULES[rule].severity
        self.artifact = artifact_rel
        self.detail = detail
        self.slug = slug
        self.num: int | None = None

    def fid(self) -> str:
        """The per-run finding ID, F1, F2, ..."""
        return f"F{self.num}" if self.num else "F?"


class Audit:
    """One audit run over a workspace or a single file."""

    def __init__(self, root: Path, as_of: date, stale_days: int, single: bool = False) -> None:
        self.root = root
        self.as_of = as_of
        self.stale_days = stale_days
        self.single = single
        self.findings: list[Finding] = []
        self._finding_keys: dict[tuple[str, str, str], Finding] = {}
        self.started_records: set[str] = set()
        # slug -> (state file, task status completed) where the RPI Agent skipped Review
        self.review_skipped: dict[str, tuple[str, bool]] = {}
        self.not_evaluated: dict[str, str] = {}
        self.artifacts: list[Artifact] = []
        self.legacy: list[Artifact] = []
        self.misnamed: list[tuple[str, str, str, str]] = []
        self.extras: list[tuple[str, str, str]] = []
        self.state_files: list[StateFile] = []
        self.state_slugs: dict[str, str | None] = {}
        self.other_areas: Counter[str] = Counter()
        self.symlinks: list[str] = []
        self.nested: list[str] = []
        self.depth_limited = False
        self.tracking_root_is_link = False
        self.tracking_root: Path | None = None
        self.gitignore_has_tracking: bool | None = None
        self.tasks: dict[str, Task] = {}
        self.legacy_tasks: dict[str, list[Artifact]] = {}
        self.board: list[dict[str, Any]] = []
        self.legacy_board: list[dict[str, Any]] = []

    # ---- bookkeeping
    def add(
        self,
        rule: str,
        artifact_rel: str,
        detail: str,
        slug: str | None = None,
        severity: str | None = None,
    ) -> Finding:
        """Record a finding once per rule, artifact, and detail."""
        key = (rule, artifact_rel, detail)
        if key in self._finding_keys:
            return self._finding_keys[key]
        f = Finding(rule, artifact_rel, detail, slug, severity)
        self._finding_keys[key] = f
        self.findings.append(f)
        return f

    def scan_secrets(self, raw: str, rel: str, slug: str | None) -> None:
        """HY1 on the first credential-shaped value, by line number only."""
        for label, pat in SECRET_PATTERNS:
            mm = pat.search(raw)
            if mm:
                line = raw.count("\n", 0, mm.start()) + 1
                self.add("HY1", rel, f"{label} pattern at line {line}", slug)
                break

    def skip(self, rule: str, reason: str) -> None:
        """Record a rule as not evaluated, with the first reason given."""
        self.not_evaluated.setdefault(rule, reason)

    # ---- entry points
    def run_workspace(self) -> None:
        """Audit the tracking folder at the workspace root."""
        roots = find_tracking_roots(self.root)
        self.tracking_root = roots.top
        self.nested = roots.nested
        self.depth_limited = roots.depth_limited
        self.tracking_root_is_link = roots.top_is_link
        if roots.top_is_link:
            self.symlinks.append(TRACKING_DIR)
        gi = self.root / ".gitignore"
        if gi.is_file():
            lines = read_text(gi).split("\n")
            self.gitignore_has_tracking = any(GITIGNORE_LINE.match(line) for line in lines)
        else:
            self.gitignore_has_tracking = False
        if self.gitignore_has_tracking is False:
            where = "" if gi.is_file() else " (no .gitignore at the root)"
            self.add(
                "LY1", ".gitignore", f"no `.copilot-tracking/` line in the root .gitignore{where}"
            )
        for n in self.nested:
            self.add(
                "LY2",
                n,
                "a .copilot-tracking folder exists below the workspace root; the RPI skills "
                "resolve only the root folder, and this audit did not read it",
            )
        if not self.stale_days:
            self.skip("TK6", "--stale-days 0")
        if roots.top is None:
            reason = (
                ".copilot-tracking at the workspace root is a symbolic link, which is not followed"
                if roots.top_is_link
                else "no .copilot-tracking/ folder at the workspace root"
            )
            for rule in RULES:
                if rule not in ("LY1", "LY2", "TK6"):
                    self.skip(rule, reason)
            return
        found = discover(self.root, roots.top)
        self.artifacts = found.artifacts
        self.legacy = found.legacy
        self.misnamed = found.misnamed
        self.extras = found.extras
        self.state_files = found.state_files
        self.other_areas = found.other_areas
        self.symlinks = sorted(set(self.symlinks) | set(found.symlinks))
        for rel, area, slug in self.extras:
            self.add("HY2", rel, f"non-Markdown file in the {area} area", slug)
        for rel, area, kind, slug in self.misnamed:
            self.add(
                "LY4",
                rel,
                f"expected `{{slug}}{SUFFIX_BY_KIND[kind]}` in {area}; "
                "the file is listed but not modeled",
                slug,
            )
        for art in self.artifacts:
            self.audit_artifact(art)
        for art in self.legacy:
            model_legacy(art)
        self.group_tasks()
        for slug, task in self.tasks.items():
            self.audit_task(slug, task)
        for slug, arts in self.legacy_tasks.items():
            lead = next((a for a in arts if a.legacy == "implementation plan"), arts[0])
            kinds = capped(sorted({a.legacy or "" for a in arts}), 4)
            self.add(
                "LY9", lead.rel, f"{plural(len(arts), 'legacy file')} for `{slug}`: {kinds}", slug
            )
        self.audit_state()
        for slug, task in self.tasks.items():
            self.audit_staleness(slug, task)
        self.build_board()
        if not self.state_files:
            for rule in STATE_RULES:
                self.skip(
                    rule,
                    "no RPI Agent session state file (JSON with task_slug, active_phase, "
                    "artifact_paths) found under .copilot-tracking/",
                )

    def run_single(self, path: Path) -> None:
        """Audit one artifact alone; cross-artifact rules are recorded as not evaluated."""
        if path.is_symlink():
            raise UsageError(f"--file refuses a symbolic link: {path}")
        abs_path = path.absolute()
        fn = abs_path.name
        kind = slug = area = None
        n = None
        for area_name, (k, pattern) in AREAS.items():
            m = pattern.match(fn)
            if m:
                kind, slug, area = k, m.group("slug"), area_name
                n = int(m.group("n")) if m.groupdict().get("n") else None
                break
        if kind is None or slug is None or area is None:
            if LEGACY_PLAN.match(fn) or fn.endswith(("-details.md", "-log.md", "-validation.md")):
                raise UsageError(
                    f"--file does not audit files in the retired Task Planner format ({fn}); "
                    "run the audit on the workspace to list them as legacy"
                )
            raise UsageError(
                "--file expects a file named {slug}-research.md, -plan.md, -plan-critique.md, "
                f"-changes.md, -review.md, -challenge.md, or -decisions.md; got {fn}"
            )
        # workspace root: the parent of the nearest .copilot-tracking ancestor, else the folder
        root = next((p.parent for p in abs_path.parents if p.name == TRACKING_DIR), abs_path.parent)
        self.root = root
        # the no-follow boundary covers every component below the workspace root
        probe = abs_path
        while probe != root and probe.parent != probe:
            if probe.is_symlink():
                raise UsageError(
                    f"--file refuses a path through a symbolic link: {relpath(probe, root)} "
                    "(the audit follows no links)"
                )
            probe = probe.parent
        date_dir = abs_path.parent.name
        art = Artifact(root, abs_path, area, kind, slug, date_dir, n)
        art.model["date_ok"] = DATE_DIR.match(date_dir) is not None
        if looks_legacy(art):
            art.kind = "legacy"
            art.legacy = LEGACY_KIND_BY_KIND.get(kind, kind)
            model_legacy(art)
            self.legacy = [art]
            self.add("LY9", art.rel, f"written in the retired format ({art.legacy})", slug)
        else:
            self.artifacts = [art]
            self.audit_artifact(art)
        for rule in RULES:
            if rule in CROSS_ARTIFACT_RULES:
                self.skip(rule, "single-file run; needs the other artifacts of the task")
            elif rule in ("LY1", "LY2", "HY2"):
                self.skip(rule, "single-file run; needs the workspace")
        self.group_tasks()
        self.build_board()

    def filter_task(self, slug: str) -> None:
        """Keep one task and the workspace-level findings, dropping other tasks' state files."""
        if slug not in self.tasks and slug not in self.legacy_tasks:
            names = ", ".join(list(self.tasks) + list(self.legacy_tasks)) or "none"
            raise UsageError(f"no task `{slug}`; tasks found: {names}")
        kept = [sf for sf in self.state_files if self.state_slugs.get(sf.rel) == slug]
        dropped = {sf.rel for sf in self.state_files} - {sf.rel for sf in kept}
        self.state_files = kept
        self.findings = [
            f
            for f in self.findings
            if f.slug == slug or (f.slug is None and f.artifact not in dropped)
        ]
        self.tasks = {slug: self.tasks[slug]} if slug in self.tasks else {}
        self.legacy_tasks = {slug: self.legacy_tasks[slug]} if slug in self.legacy_tasks else {}
        self.artifacts = [a for a in self.artifacts if a.slug == slug]
        self.legacy = [a for a in self.legacy if a.slug == slug]
        self.misnamed = [m for m in self.misnamed if m[3] == slug]
        self.extras = [e for e in self.extras if e[2] == slug]
        self.board = [r for r in self.board if r["slug"] == slug]
        self.legacy_board = [r for r in self.legacy_board if r["slug"] == slug]

    # ---- per-artifact rules
    def audit_artifact(self, art: Artifact) -> None:
        """Layout, template, and secret rules, then the kind's own rules."""
        rel, slug = art.rel, art.slug
        if not art.model.get("date_ok"):
            self.add("LY3", rel, f"expected {art.area}/YYYY-MM-DD/{art.path.name}", slug)
        if slug and not KEBAB.match(slug):
            self.add("LY5", rel, f"slug `{slug}` is not lower-kebab-case", slug)
        elif slug and len(slug) > SLUG_MAX:
            self.add("LY5", rel, f"slug is {len(slug)} characters; the cap is {SLUG_MAX}", slug)
        if not art.text.strip():
            art.unrecognized = True
            error = getattr(art, "read_error", None)
            detail = f"the file cannot be read ({error})" if error else "the file is empty"
            self.add("LY10", rel, detail, slug)
            return
        required = REQUIRED_BY_KIND.get(art.kind)
        if required and not any(art.doc.has_section(s) for s in required):
            art.unrecognized = True
            sections = capped([f"`## {s}`" for s in required], 3)
            self.add(
                "LY10",
                rel,
                f"none of the sections a {KIND_LABEL[art.kind]} carries ({sections}) is present",
                slug,
            )
        first = art.doc.lines[0].strip() if art.doc.lines else ""
        if "markdownlint-disable-file" not in first:
            self.add("LY6", rel, f"first line is `{short(first, 50)}`", slug)
        body = strip_fences_and_comments(art.text)
        ref = line_number_reference(body)
        if ref:
            self.add("LY7", rel, f"`{short(ref, 50)}`", slug)
        if FILE_DIRECTIVE.search(body):
            self.add("LY8", rel, "`#file:` used in a tracking artifact", slug)
        self.scan_secrets(art.raw_text, rel, slug)
        placeholders = sorted(set(PLACEHOLDER.findall(body)))
        if art.unrecognized:
            self._report_placeholders(art, placeholders)
            return
        auditors = {
            "plan": self.audit_plan,
            "critique": self.audit_critique,
            "changes": self.audit_changes,
            "review": self.audit_review,
            "research": self.audit_research,
        }
        if art.kind in auditors:
            auditors[art.kind](art)
        # a critique or review saved while `started` still carries its template's placeholders
        if not art.model.get("started"):
            self._report_placeholders(art, placeholders)

    def _report_placeholders(self, art: Artifact, placeholders: list[str]) -> None:
        if not placeholders:
            return
        sev = "medium" if art.kind in ("plan", "changes", "review", "critique") else "low"
        shown = capped([f"`{short(p, 40)}`" for p in placeholders], 3)
        self.add(
            "PL9",
            art.rel,
            f"{plural(len(placeholders), 'placeholder')}: {shown}",
            art.slug,
            severity=sev,
        )

    def check_sections(self, art: Artifact) -> None:
        """PL10 for each required H2 the artifact lacks."""
        for sec in REQUIRED_BY_KIND[art.kind]:
            if not art.doc.has_section(sec):
                self.add(
                    "PL10", art.rel, f"no `## {sec}` section ({KIND_LABEL[art.kind]})", art.slug
                )

    def audit_plan(self, art: Artifact) -> None:
        """Markers, headings, blocks, requirements, readiness, critique, and decisions."""
        m = model_plan(art)
        rel, slug = art.rel, art.slug
        self.check_sections(art)
        titles = [t.strip() for t in m["h2"]]
        if (
            "Follow-Up Items" in titles
            and "Handoff" in titles
            and titles.index("Handoff") != titles.index("Follow-Up Items") + 1
        ):
            self.add("PL11", rel, "`## Follow-Up Items` is not directly before `## Handoff`", slug)
        for mid, i in m["orphan_markers"]:
            self.add(
                "PL1",
                rel,
                f"marker `{mid}` at line {i + 1} is not followed by a heading with that ID",
                slug,
            )
        ids = Counter([p["id"] for p in m["phases"]] + [t["id"] for t in m["tasks"]])
        for dup, c in ids.items():
            if c > 1:
                self.add("PL3", rel, f"`{dup}` appears {c} times", slug)
        for p in m["phases"]:
            pid = p["id"]
            if p["marker"] is None:
                self.add(
                    "PL2",
                    rel,
                    f"phase `{pid}` heading has no `<!-- rpi:phase id={pid} -->` marker before it",
                    slug,
                )
            elif p["marker"] != ("phase", pid):
                self.add(
                    "PL1", rel, f"phase `{pid}` is preceded by marker `{p['marker'][1]}`", slug
                )
            for blk in ("Goals", "Dependencies", "Changes in the map"):
                if blk not in p["blocks"]:
                    self.add("PL8", rel, f"phase `{pid}` has no `{blk}:` line or block", slug)
            if p["checked"]:
                unchecked = sorted(
                    {t["id"] for t in m["tasks"] if t["enclosing"] == pid and not t["checked"]}
                )
                if unchecked:
                    self.add(
                        "PL5",
                        rel,
                        f"phase `{pid}` is checked but {capped(unchecked)} unchecked",
                        slug,
                    )
        for t in m["tasks"]:
            tid = t["id"]
            if t["marker"] is None:
                self.add(
                    "PL2",
                    rel,
                    f"task `{tid}` heading has no `<!-- rpi:task id={tid} -->` marker before it",
                    slug,
                )
            elif t["marker"] != ("task", tid):
                self.add("PL1", rel, f"task `{tid}` is preceded by marker `{t['marker'][1]}`", slug)
            if t["enclosing"] and t["enclosing"] != t["phase"]:
                self.add("PL4", rel, f"task `{tid}` sits under phase `{t['enclosing']}`", slug)
            missing = [
                b
                for b in ("Goals", "Requirements", "Details", "References", "Dependencies")
                if b not in t["blocks"]
            ]
            if missing:
                self.add(
                    "PL7", rel, f"task `{tid}` lacks {capped([f'`{b}:`' for b in missing])}", slug
                )
        defined, cited = set(m["req_defined"]), set(m["req_cited"])
        for r in sorted(defined - cited):
            self.add("PL12", rel, f"`{r}` is defined but no task cites it", slug)
        if defined:
            for r in sorted(cited - defined):
                self.add("PL13", rel, f"`{r}` is cited but not defined", slug)
        meta_slug = m["task_slug"]
        if meta_slug and not is_placeholder_value(meta_slug) and meta_slug.strip("`") != slug:
            self.add(
                "PL17", rel, f"Task slug `{meta_slug}` in metadata, `{slug}` in the file name", slug
            )
        if m["readiness"] in ("Not ready", "Blocked"):
            blockers = short(m["blockers_raw"] or "(not recorded)", 70)
            self.add("PL19", rel, f"readiness `{m['readiness']}`; blockers: {blockers}", slug)
        for grp, item, status in m["open_decisions"]:
            self.add("PL16", rel, f"{grp} `{item}` is {short(status, 30)}", slug)
        if (
            m["readiness"] == "Ready"
            and not m["critique_skipped"]
            and m["critique_verdict"] is None
        ):
            status = short(m["critique_status"] or "(nothing)", 40)
            self.add(
                "PL15",
                rel,
                f"readiness is Ready, Critique Disposition records `{status}`, "
                "no Pass, Revise, or Blocked verdict and no skip",
                slug,
            )

    def audit_critique(self, art: Artifact) -> None:
        """Verdict, execution, finding markers, reconciliation, and the plan link."""
        m = model_critique(art)
        rel, slug = art.rel, art.slug
        self.check_sections(art)
        if m["started"]:
            self.add(
                "PC6", rel, "critique execution is `started`; the verdict is not recorded yet", slug
            )
        elif m["verdict"] is None:
            self.add("PC2", rel, f"verdict is `{short(m['verdict_raw'] or '(missing)', 40)}`", slug)
        if m["execution"] is None:
            self.add(
                "PC5",
                rel,
                f"critique execution is `{short(m['execution_raw'] or '(missing)', 40)}`",
                slug,
            )
        for d, c in Counter(m["finding_ids"]).items():
            if c > 1:
                self.add("PC1", rel, f"`{d}` heading repeats", slug)
        for fid in m["finding_ids"]:
            if fid not in m["marker_ids"]:
                self.add(
                    "PC1", rel, f"`{fid}` has no `<!-- rpi:critique id={fid} -->` marker", slug
                )
        for mid in m["marker_ids"]:
            if mid not in m["finding_ids"]:
                self.add("PC1", rel, f"marker `{mid}` has no finding heading", slug)
        follow_up = art.suffix_n or (
            not is_placeholder_value(m["type"]) and re.match(r"\s*follow", m["type"], re.I)
        )
        if follow_up and not m["has_reconciliation"]:
            self.add(
                "PC4", rel, "follow-up critique without `## Earlier Finding Reconciliation`", slug
            )
        ref = resolve_ref(m["plan_ref"], art.path.parent, art.root)
        if ref and not ref[1] and not self.single:
            self.add("PC3", rel, f"Plan `{ref[0]}` not found", slug)

    def audit_changes(self, art: Artifact) -> None:
        """Status, validation, alignment, blockers, review readiness, and the plan link."""
        m = model_changes(art)
        rel, slug = art.rel, art.slug
        self.check_sections(art)
        if m["status"] is None:
            self.add("CH3", rel, f"status is `{short(m['status_raw'] or '(missing)', 40)}`", slug)
        for e in m["entries"]:
            if is_placeholder_value(e["validation"]):
                related = ", ".join(e["related"]) or "no marker"
                self.add(
                    "CH5",
                    rel,
                    f"entry `{short(e['title'], 40)}` ({related}) has no validation result",
                    slug,
                )
        for cells in m["validation_rows"]:
            if re.match(r"^(failed|unavailable)", cells[2], re.I):
                check = short(cells[0], 40)
                check = check if check.startswith("`") else f"`{check}`"
                self.add("CH10", rel, f"check {check} is {cells[2].split()[0]}", slug)
        if "pending-decision" in m["alignment_values"]:
            self.add("CH7", rel, "a Business Alignment entry is `pending-decision`", slug)
        for b in m["blockers"]:
            self.add("CH8", rel, short(b, 80), slug)
        if m["review_ready"] and (m["blockers"] or m["remaining_work"]):
            self.add(
                "CH9",
                rel,
                f"review readiness `{short(m['review_readiness_raw'], 30)}` with "
                f"{plural(len(m['blockers']), 'blocker')} and "
                f"{plural(len(m['remaining_work']), 'remaining item')} listed",
                slug,
            )
        ref = resolve_ref(m["plan_ref"], art.path.parent, art.root)
        if ref and not ref[1] and not self.single:
            self.add("CH6", rel, f"Related plan `{ref[0]}` not found", slug)

    def audit_review(self, art: Artifact) -> None:
        """Execution, outcomes, finding markers, routes, decisions, and evidence links."""
        m = model_review(art)
        rel, slug = art.rel, art.slug
        self.check_sections(art)
        if m["started"]:
            self.add(
                "RV8",
                rel,
                "review execution is `started`; the finding set is not complete yet",
                slug,
            )
        elif m["execution"] is None or m["outcome"] is None:
            execution = short(m["execution_raw"] or "(missing)", 30)
            outcome = short(m["outcome_raw"] or "(missing)", 40)
            self.add("RV3", rel, f"execution `{execution}`, assessed outcome `{outcome}`", slug)
        ids = [f["id"] for f in m["findings"]]
        for d, c in Counter(ids).items():
            if c > 1:
                self.add("RV1", rel, f"`{d}` heading repeats", slug)
        for fid in ids:
            if fid not in m["marker_ids"]:
                self.add("RV1", rel, f"`{fid}` has no `<!-- rpi:review id={fid} -->` marker", slug)
        for mid in m["marker_ids"]:
            if mid not in ids:
                self.add("RV1", rel, f"marker `{mid}` has no finding heading", slug)
        final = None if m["final_pending"] else m["final_outcome"]
        claim = " and ".join(
            f"{label} outcome {value}"
            for label, value in (("assessed", m["outcome"]), ("final", final))
            if value in ("Conformant", "Conformant with justified divergence")
        )
        for f in m["findings"]:
            fid, sev = f["id"], f["severity"]
            if f["destination"] is None and not m["started"]:
                dest = short(f["destination_raw"] or "(missing)", 40)
                self.add("RV2", rel, f"`{fid}` destination is `{dest}`", slug)
            material = bool(claim) and sev in ("Critical", "High")
            decision = m["decisions"].get(fid, "")
            if fid not in m["decided"] and not m["started"]:
                self.add(
                    "RV4", rel, f"`{fid}` [{sev or '?'}] has no RD event in Decision History", slug
                )
                if material:
                    self.add(
                        "RV6",
                        rel,
                        f"`{fid}` [{sev}] undecided with {claim}",
                        slug,
                    )
            elif material and fid in m["decided"] and MATERIAL_OPEN.search(decision):
                self.add(
                    "RV6",
                    rel,
                    f"`{fid}` [{sev}] decision `{short(decision, 30)}` with {claim}",
                    slug,
                )
        if m["final_pending"] and m["execution"] == "Complete":
            final = short(m["final_outcome_raw"] or "(not recorded)", 30)
            self.add(
                "RV7",
                rel,
                f"Final outcome `{final}`; assessed outcome `{m['outcome'] or '?'}`",
                slug,
            )
        for label, raw in (("Plan", m["plan_ref"]), ("Changes", m["changes_ref"])):
            ref = resolve_ref(raw, art.path.parent, art.root)
            if ref and not ref[1] and not self.single:
                self.add("RV5", rel, f"{label} `{ref[0]}` not found", slug)

    def audit_research(self, art: Artifact) -> None:
        """Readiness, disposition, and evidence citations."""
        m = model_research(art)
        rel, slug = art.rel, art.slug
        self.check_sections(art)
        if m["readiness"] is None:
            self.add(
                "RS1",
                rel,
                f"Planning Readiness is `{short(m['readiness_raw'] or '(missing)', 40)}`",
                slug,
            )
        elif m["readiness"] in ("Not ready", "Blocked"):
            blockers = short(m["blockers_raw"] or "(not recorded)", 60)
            self.add(
                "RS4", rel, f"Planning Readiness `{m['readiness']}`; blockers: {blockers}", slug
            )
        if m["disposition"] is None:
            disposition = short(m["disposition_raw"] or "(missing)", 40)
            self.add("RS3", rel, f"Research disposition is `{disposition}`", slug)
        undefined = sorted(set(m["evidence_cited"]) - set(m["evidence_defined"]))
        if undefined and m["evidence_defined"]:
            shown = capped([f"`{u}`" for u in undefined])
            self.add("RS2", rel, f"cited but not in the Evidence Log: {shown}", slug)

    # ---- task grouping and cross-artifact rules
    def group_tasks(self) -> None:
        """Group recognized artifacts by slug, oldest first within each kind."""
        tasks: dict[str, Task] = {}
        for art in sorted(self.artifacts, key=lambda a: (a.slug, a.sort_key())):
            if art.unrecognized:
                continue
            tasks.setdefault(art.slug, {k: [] for k in KIND_ORDER})[art.kind].append(art)
        self.tasks = dict(sorted(tasks.items()))
        legacy: dict[str, list[Artifact]] = {}
        for art in sorted(self.legacy, key=lambda a: (a.slug, a.sort_key())):
            legacy.setdefault(art.slug, []).append(art)
        self.legacy_tasks = dict(sorted(legacy.items()))

    @staticmethod
    def latest(task: Task, kind: str) -> Artifact | None:
        """The newest artifact of a kind."""
        return task[kind][-1] if task[kind] else None

    @staticmethod
    def plan_fully_checked(plan: Artifact | None) -> bool:
        """Whether the plan has tasks and every task is checked."""
        return (
            plan is not None
            and bool(plan.model["tasks"])
            and all(t["checked"] for t in plan.model["tasks"])
        )

    def audit_task(self, slug: str, task: Task) -> None:
        """Rules that compare one task's artifacts with each other."""
        plan, changes, review, critique, research = (
            self.latest(task, k) for k in ("plan", "changes", "review", "critique", "research")
        )
        for kind in ("plan", "changes", "research"):
            if len(task[kind]) > 1:
                files = capped([a.rel for a in task[kind]], 3)
                label = plural(len(task[kind]), KIND_LABEL[kind] + " file")
                self.add("TK5", task[kind][-1].rel, f"{label} for `{slug}`: {files}", slug)
        # findings on a superseded critique or review are history, not work to do
        for kind in ("critique", "review"):
            for older in task[kind][:-1]:
                for f in self.findings:
                    if f.artifact == older.rel and f.severity != "info" and f.rule != "HY1":
                        f.severity = "info"
                        f.detail += f" (superseded by {task[kind][-1].rel})"
        if changes and not plan:
            self.add(
                "TK1", changes.rel, f"no `{slug}-plan.md` under .copilot-tracking/plans/", slug
            )
        if review and not changes:
            self.add(
                "TK2", review.rel, f"no `{slug}-changes.md` under .copilot-tracking/changes/", slug
            )
        if critique and not plan:
            self.add(
                "TK3", critique.rel, f"no `{slug}-plan.md` under .copilot-tracking/plans/", slug
            )
        if research and not plan and not changes and not review:
            readiness = research.model.get("readiness") or "(not recorded)"
            self.add("TK10", research.rel, f"research only; readiness `{readiness}`", slug)
        ids: dict[str, str] = {}
        for a in (plan, critique, changes, review):
            if a is not None:
                tid = a.model.get("task_id", "")
                if tid and not is_placeholder_value(tid):
                    ids[a.kind] = tid.strip("`")
        if len(set(ids.values())) > 1:
            anchor = plan or changes or review or critique
            if anchor is not None:
                self.add("TK4", anchor.rel, "; ".join(f"{k} `{v}`" for k, v in ids.items()), slug)
        if plan is not None:
            self._audit_plan_links(slug, plan, changes, critique)

    def audit_staleness(self, slug: str, task: Task) -> None:
        """TK6, after the state join so a task completed without Review is closed."""
        plan, research, changes, review = (
            self.latest(task, k) for k in ("plan", "research", "changes", "review")
        )
        latest_change = max((a.mtime for k in KIND_ORDER for a in task[k] if a.mtime), default=None)
        if (
            latest_change
            and self.stale_days
            and not self.task_accepted(task)
            and not self.completed_without_review(slug, task)
            and (self.as_of - latest_change).days > self.stale_days
        ):
            anchor = plan or research or changes or review
            anchor = anchor or next(task[k][-1] for k in KIND_ORDER if task[k])
            days = (self.as_of - latest_change).days
            self.add(
                "TK6",
                anchor.rel,
                f"last change {latest_change.isoformat()}, {days} days before the reference date",
                slug,
            )

    def _audit_plan_links(
        self, slug: str, plan: Artifact, changes: Artifact | None, critique: Artifact | None
    ) -> None:
        pm = plan.model
        task_ids = {t["id"] for t in pm["tasks"]}
        plan_ids = task_ids | {p["id"] for p in pm["phases"]}
        checked_tasks = {t["id"] for t in pm["tasks"] if t["checked"]}
        if changes is not None:
            cm = changes.model
            evidenced = set(cm["entry_markers"]) | set(cm["completed_markers"])
            for mid in sorted(set(cm["all_mentioned"]) - plan_ids):
                self.add("CH1", changes.rel, f"`{mid}` is not a phase or task in {plan.rel}", slug)
            for tid in sorted(checked_tasks - evidenced):
                self.add(
                    "PL6", plan.rel, f"`{tid}` is checked; {changes.rel} has no entry for it", slug
                )
            for tid in sorted((evidenced & task_ids) - checked_tasks):
                self.add(
                    "CH2",
                    changes.rel,
                    f"`{tid}` is recorded as completed; its plan marker is unchecked",
                    slug,
                )
            unchecked = sorted(task_ids - checked_tasks)
            if cm["status"] == "Complete" and cm["full_plan_scope"] and unchecked:
                self.add(
                    "CH4",
                    changes.rel,
                    f"status Complete for the full plan; unchecked: {capped(unchecked)}",
                    slug,
                )
        elif checked_tasks:
            self.add(
                "PL6",
                plan.rel,
                f"{capped(sorted(checked_tasks))} checked and no changes record exists "
                f"for `{slug}`",
                slug,
            )
        if (
            critique is None
            and pm["critique_verdict"]
            and pm["critique_path_claimed"]
            and not pm["critique_skipped"]
        ):
            recorded = short(pm["critique_latest"] or pm["latest_critique_raw"], 60)
            self.add(
                "PL14",
                plan.rel,
                f"plan records `{recorded}`; no critique file under "
                f".copilot-tracking/reviews/plans/ for `{slug}`",
                slug,
            )
        if critique is not None and not critique.model.get("started"):
            for pc in critique.model["finding_ids"]:
                if pc not in pm["pc_disposed"]:
                    sev = critique.model["severities"].get(pc) or "?"
                    self.add(
                        "PL18",
                        plan.rel,
                        f"`{pc}` [{sev}] from {critique.rel} has no disposition row",
                        slug,
                    )

    def task_accepted(self, task: Task) -> bool:
        """Whether the latest full-scope review records a Conformant final outcome."""
        review, plan, changes = (self.latest(task, k) for k in ("review", "plan", "changes"))
        if review is None:
            return False
        rm = review.model
        if (
            rm["execution"] != "Complete"
            or rm["final_pending"]
            or rm["final_outcome"] not in ("Conformant", "Conformant with justified divergence")
        ):
            return False
        if rm["bounded_scope"] or plan is None or changes is None:
            return False
        if any(
            f["severity"] in ("Critical", "High")
            and (
                f["id"] not in rm["decided"]
                or MATERIAL_OPEN.search(rm["decisions"].get(f["id"], ""))
            )
            for f in rm["findings"]
        ):
            return False
        return self.plan_fully_checked(plan)

    def completed_without_review(self, slug: str, task: Task) -> bool:
        """Whether session state records the task completed with the Review step skipped."""
        skipped = self.review_skipped.get(slug)
        return (
            skipped is not None
            and skipped[1]
            and self.latest(task, "review") is None
            and self.plan_fully_checked(self.latest(task, "plan"))
        )

    # ---- session state
    def state_slug(self, sf: StateFile, by_rel: dict[str, Artifact]) -> str | None:
        """The task a state file tracks: the slug of an artifact it points at, else task_slug.

        Later work in a session names its documents for the work instead of the task and may
        keep earlier research it reuses, so artifact_paths is read newest kind first.
        """
        paths = sf.state.get("artifact_paths")
        if isinstance(paths, dict):
            for kind in STATE_JOIN_ORDER:
                p = paths.get(kind)
                if isinstance(p, str) and p in by_rel:
                    return by_rel[p].slug
        slug = sf.state.get("task_slug")
        return slug if isinstance(slug, str) and slug else None

    def audit_state(self) -> None:
        """Session state rules: paths, phase, completion, location, links, and documents."""
        by_rel = {a.rel: a for a in self.artifacts + self.legacy}
        states_by_rel = {sf.rel: sf for sf in self.state_files}
        self.started_records = {a.rel for a in self.artifacts if a.model.get("started")}
        for sf in self.state_files:
            st, rel = sf.state, sf.rel
            slug = self.state_slug(sf, by_rel)
            self.state_slugs[rel] = slug
            self.scan_secrets(sf.raw_text, rel, slug)
            self._audit_state_paths(st, rel, slug)
            self._audit_state_location(rel, slug)
            self._audit_state_links(st, rel, slug, states_by_rel)
            task = self.tasks.get(slug) if slug else None
            if slug and review_step_skipped(st):
                self.review_skipped[slug] = (rel, st.get("status") == "completed")
            self._audit_state_phase(st, rel, slug, task)
            if st.get("status") == "completed" and task is not None and task["plan"]:
                unchecked = [t["id"] for t in task["plan"][-1].model["tasks"] if not t["checked"]]
                if unchecked:
                    self.add("TK9", rel, f"status completed; unchecked: {capped(unchecked)}", slug)

    def _audit_state_phase(
        self, st: dict[str, Any], rel: str, slug: str | None, task: Task | None
    ) -> None:
        nxt = st.get("next_action") if isinstance(st.get("next_action"), dict) else {}
        target = nxt.get("phase") or st.get("active_phase")
        if target not in ("Implement", "Review", "Follow-up"):
            return
        if task is None:
            if slug in self.legacy_tasks:
                return
            self.add(
                "TK8",
                rel,
                f"phase `{target}` recorded; no recognized artifacts for "
                f"`{slug or '(no task_slug)'}` under .copilot-tracking/",
                slug,
            )
        elif not task["plan"]:
            self.add("TK8", rel, f"phase `{target}` recorded, no plan for `{slug}`", slug)
        elif target in ("Review", "Follow-up") and not task["changes"]:
            self.add("TK8", rel, f"phase `{target}` recorded, no changes record for `{slug}`", slug)

    def _audit_state_paths(self, st: dict[str, Any], rel: str, slug: str | None) -> None:
        paths = st.get("artifact_paths")
        paths = paths if isinstance(paths, dict) else {}
        docs = st.get("documents")
        docs = docs if isinstance(docs, dict) else None
        for key, p in paths.items():
            if not isinstance(p, str) or not p:
                continue
            listed = docs.get(key) if docs is not None else None
            listed = listed if isinstance(listed, list) else None
            exists = workspace_file(self.root, p)
            # the agent records a critique or review path before the record is first written
            # and lists it in documents once the phase returns
            if not exists and key in ("critique", "review") and (listed is None or p not in listed):
                continue
            if not exists:
                self.add("TK7", rel, f"artifact_paths.{key} = `{p}` does not exist", slug)
            if listed is not None and p not in listed and p not in self.started_records:
                self.add(
                    "TK13", rel, f"artifact_paths.{key} = `{p}` is not in documents.{key}", slug
                )
        if docs is None:
            return
        for key, listed in docs.items():
            if not isinstance(listed, list):
                continue
            for p in listed:
                if (
                    isinstance(p, str)
                    and p
                    and p != paths.get(key)
                    and not workspace_file(self.root, p)
                ):
                    self.add("TK7", rel, f"documents.{key} lists `{p}`, which does not exist", slug)

    def _audit_state_location(self, rel: str, slug: str | None) -> None:
        parts = rel.split("/")
        in_session_folder = (
            len(parts) == 5
            and parts[0] == TRACKING_DIR
            and parts[1] == SESSIONS_AREA
            and DATE_DIR.match(parts[2]) is not None
            and STATE_FILE.match(parts[4]) is not None
        )
        if not in_session_folder:
            self.add(
                "TK11",
                rel,
                "expected .copilot-tracking/rpi-sessions/YYYY-MM-DD/{session}/"
                "{task_slug}-state.json",
                slug,
            )

    def _audit_state_links(
        self,
        st: dict[str, Any],
        rel: str,
        slug: str | None,
        states_by_rel: dict[str, StateFile],
    ) -> None:
        parent = st.get("parent_task")
        if isinstance(parent, dict) and isinstance(parent.get("state_path"), str):
            ppath = parent["state_path"]
            if not workspace_file(self.root, ppath):
                self.add("TK12", rel, f"parent_task.state_path `{ppath}` does not exist", slug)
            elif ppath in states_by_rel:
                children = states_by_rel[ppath].state.get("child_tasks")
                children = children if isinstance(children, list) else []
                listed = [c.get("state_path") for c in children if isinstance(c, dict)]
                if rel not in listed:
                    self.add(
                        "TK12",
                        rel,
                        f"parent `{ppath}` does not list this state in child_tasks",
                        slug,
                    )
        children = st.get("child_tasks")
        if not isinstance(children, list):
            return
        for child in children:
            cpath = child.get("state_path") if isinstance(child, dict) else None
            # a listed child's state may not exist yet: the agent records it before creating it
            if not isinstance(cpath, str) or cpath not in states_by_rel:
                continue
            back = states_by_rel[cpath].state.get("parent_task")
            back_path = back.get("state_path") if isinstance(back, dict) else None
            if back_path != rel:
                self.add(
                    "TK12",
                    rel,
                    f"child `{cpath}` names parent `{back_path or 'none'}`, not this state",
                    slug,
                )

    # ---- task board
    def build_board(self) -> None:
        """One row per task, then one per legacy task."""
        for slug, task in self.tasks.items():
            self.board.append(self.task_row(slug, task))
        for slug, arts in self.legacy_tasks.items():
            plan = next((a for a in arts if a.legacy == "implementation plan"), None)
            if plan:
                progress = f"{plan.model['steps_checked']}/{plan.model['steps_total']} steps"
                nxt = f"re-plan with `/rpi-plan task={slug} draft={plan.rel}`"
            else:
                progress = "no plan file"
                nxt = f"re-plan with `/rpi-plan task={slug}`, citing the legacy files as context"
            self.legacy_board.append(
                {
                    "slug": slug,
                    "files": [a.rel for a in arts],
                    "kinds": sorted({a.legacy or "" for a in arts}),
                    "progress": progress,
                    "last_change": max((a.mtime for a in arts if a.mtime), default=None),
                    "next": nxt,
                }
            )

    def task_row(self, slug: str, task: Task) -> dict[str, Any]:
        """The board row: state, progress, readiness, execution, review, and next action."""
        plan, changes, review, research = (
            self.latest(task, k) for k in ("plan", "changes", "review", "research")
        )
        row: dict[str, Any] = {
            "slug": slug,
            "artifacts": {k: [a.rel for a in task[k]] for k in KIND_ORDER if task[k]},
        }
        row["phase_reached"] = (
            "Review"
            if review
            else "Implement"
            if changes
            else "Plan"
            if plan
            else "Research"
            if research
            else "none"
        )
        if plan:
            pm = plan.model
            total = len(pm["tasks"])
            done = sum(1 for t in pm["tasks"] if t["checked"])
            row["plan_progress"] = f"{done}/{total} tasks" if total else "no tasks"
            row["readiness"] = pm["readiness"] or "(not recorded)"
            row["open_decisions"] = len(pm["open_decisions"])
            row["follow_ups"] = len(pm["follow_ups"])
            row["next_task"] = self.next_task(pm)
        else:
            row["plan_progress"] = "-"
            row["readiness"] = (
                (research.model["readiness"] or "(not recorded)") if research else "-"
            )
            row["open_decisions"] = 0
            row["follow_ups"] = 0
            row["next_task"] = None
        row["execution"] = (changes.model["status"] or "(not recorded)") if changes else "-"
        row["blockers"] = len(changes.model["blockers"]) if changes else 0
        if review:
            rm = review.model
            execution = STARTED if rm["started"] else (rm["execution"] or "?")
            row["review"] = f"{execution} / {rm['outcome'] or '?'}"
            row["final_outcome"] = "pending" if rm["final_pending"] else rm["final_outcome"]
            row["undecided"] = [f["id"] for f in rm["findings"] if f["id"] not in rm["decided"]]
            row["routes"] = dict(
                Counter(
                    f["route"] or ("none" if f["final_destination"] == "none" else "unrouted")
                    for f in rm["findings"]
                )
            )
            row["review_scope"] = "bounded" if rm["bounded_scope"] else "full"
        else:
            row["review"] = "-"
            row["final_outcome"] = "-"
            row["undecided"] = []
            row["routes"] = {}
            row["review_scope"] = "-"
        row["last_change"] = max(
            (a.mtime for k in KIND_ORDER for a in task[k] if a.mtime), default=None
        )
        row["state"] = self.task_state(task, row)
        row["next"] = self.next_command(slug, task, row)
        high_arts = sorted(
            {f.artifact for f in self.findings if f.severity == "high" and f.slug == slug}
        )
        if high_arts:
            row["next"] = (
                f"repair the high findings on {capped(high_arts, 2)} first, then: {row['next']}"
            )
        if changes and changes.model["blockers"] and changes.model["status"] != "Blocked":
            row["next"] += f" (a blocker is recorded in {changes.rel})"
        return row

    @staticmethod
    def next_task(pm: dict[str, Any]) -> str | None:
        """The first unchecked task whose dependencies are checked."""
        checked = {t["id"] for t in pm["tasks"] if t["checked"]}
        for t in pm["tasks"]:
            if not t["checked"] and all(d in checked for d in t["deps"]):
                return t["id"]
        for t in pm["tasks"]:
            if not t["checked"]:
                return t["id"] + " (dependencies unchecked)"
        return None

    def task_state(self, task: Task, row: dict[str, Any]) -> str:
        """The task's state word, derived from review, then changes, then plan, then research."""
        plan, changes, review, research, critique = (
            self.latest(task, k) for k in ("plan", "changes", "review", "research", "critique")
        )
        if self.task_accepted(task):
            return "accepted"
        if (
            review
            and not review.model["bounded_scope"]
            and (self.plan_fully_checked(plan) or plan is None)
        ):
            rm = review.model
            if plan is None or changes is None:
                return "reviewed, evidence missing"
            if rm["started"]:
                return "review in progress"
            if row["undecided"] or rm["outcome"] in (
                "Defects found",
                "Residual work",
                "Not accepted",
            ):
                return "reviewed, findings open"
            if rm["execution"] == "Complete" and rm["final_pending"]:
                return "reviewed, decision pending"
            return f"review {(rm['execution'] or 'incomplete').lower()}"
        if changes and changes.model["status"] == "Blocked":
            return "blocked in implementation"
        if self.plan_fully_checked(plan):
            if review is None and row["slug"] in self.review_skipped:
                if self.review_skipped[row["slug"]][1]:
                    return "completed, review skipped"
                return "implemented, review skipped"
            return "implemented, not reviewed"
        if changes:
            return "implementing"
        if plan:
            if critique is not None and critique.model.get("started"):
                return "planning, critique in progress"
            return "planned" if plan.model["readiness"] == "Ready" else "planning"
        if research:
            if research.model["readiness"] == "Not applicable":
                return "researched, no handoff"
            return "researched" if research.model["readiness"] == "Ready" else "researching"
        return "unknown"

    def next_command(self, slug: str, task: Task, row: dict[str, Any]) -> str:
        """The command the artifacts' own fields imply; a suggestion to confirm."""
        plan, changes, review, research, critique = (
            self.latest(task, k) for k in ("plan", "changes", "review", "research", "critique")
        )
        state = row["state"]
        plan_rel = plan.rel if plan else "<plan>"
        changes_arg = f" changes={changes.rel}" if changes else ""
        if state == "accepted":
            return "none; task accepted in the Parent Decision Record"
        if state.startswith("review") and review:
            return self._review_command(slug, review, plan, changes, row)
        if changes and changes.model["status"] == "Blocked":
            nt = row.get("next_task")
            blocker = (
                short(changes.model["blockers"][0], 50)
                if changes.model["blockers"]
                else "see Blockers"
            )
            task_arg = f" task={nt}" if nt and "(" not in nt else ""
            return f"clear the blocker ({blocker}), then /rpi-implement plan={plan_rel}{task_arg}"
        if state.endswith("review skipped"):
            return (
                f"none; the Review step is `skip` in {self.review_skipped[slug][0]}; run "
                f"/rpi-review task={slug} plan={plan_rel}{changes_arg} only if a review is wanted"
            )
        if plan:
            pm = plan.model
            if not pm["tasks"]:
                return f"/rpi-plan task={slug} (the plan has no tasks)"
            if self.plan_fully_checked(plan):
                return f"/rpi-review task={slug} plan={plan.rel}{changes_arg}"
            if state == "planning, critique in progress" and critique is not None:
                return (
                    f"/rpi-plan task={slug} "
                    f"(the critique in {critique.rel} was started and not finished)"
                )
            if pm["readiness"] in ("Not ready", "Blocked"):
                return f"/rpi-plan task={slug} (readiness {pm['readiness']})"
            if pm["readiness"] is None and changes is None:
                return f"/rpi-plan task={slug} (readiness not recorded)"
            if pm["open_decisions"]:
                count = plural(len(pm["open_decisions"]), "open decision")
                return f"resolve {count} in {plan.rel}, then /rpi-implement plan={plan.rel}"
            nt = row.get("next_task")
            if nt and "(" in nt:
                return f"/rpi-implement plan={plan.rel} (first unchecked task {nt})"
            return f"/rpi-implement plan={plan.rel}" + (f" task={nt}" if nt else "")
        if research:
            rm = research.model
            if rm["readiness"] == "Not applicable":
                return "none; Planning Readiness is Not applicable, so research hands off no plan"
            if rm["readiness"] == "Ready":
                return f"/rpi-plan task={slug} research={research.rel}"
            return f"/rpi-research topic={slug} (readiness {rm['readiness'] or 'not recorded'})"
        return "none derivable"

    def _review_command(
        self,
        slug: str,
        review: Artifact,
        plan: Artifact | None,
        changes: Artifact | None,
        row: dict[str, Any],
    ) -> str:
        rm = review.model
        plan_rel = plan.rel if plan else "<plan>"
        changes_arg = f" changes={changes.rel}" if changes else ""
        if row["state"] == "reviewed, evidence missing":
            missing = (
                "plan and changes record"
                if plan is None and changes is None
                else "plan"
                if plan is None
                else "changes record"
            )
            return (
                f"restore the {missing} the review compares against ({review.rel}), "
                "then rerun the audit"
            )
        if rm["started"]:
            return (
                f"/rpi-review task={slug} plan={plan_rel}{changes_arg} "
                f"(the review in {review.rel} was started and not finished)"
            )
        if row["undecided"]:
            undecided = capped(row["undecided"], 3)
            return f"decide {undecided} in the Parent Decision Record of {review.rel}, then route"
        if rm["execution"] != "Complete":
            execution = rm["execution"] or "incomplete"
            return f"/rpi-review task={slug} plan={plan_rel}{changes_arg} (review {execution})"

        def decided_as(f: dict[str, Any], word: str) -> bool:
            return re.search(rf"\b{word}\b", rm["decisions"].get(f["id"], ""), re.I) is not None

        # a rejected finding is closed; a deferred one waits on the evidence its event names
        deferred = [f["id"] for f in rm["findings"] if decided_as(f, "deferred")]
        live = [
            f for f in rm["findings"] if not decided_as(f, "rejected") and f["id"] not in deferred
        ]
        dests = [d for d in DESTINATIONS if any(f["route"] == d for f in live)]
        cmds = []
        if deferred:
            cmds.append(
                f"gather the evidence the Decision History of {review.rel} names for deferred "
                f"{capped(deferred, 3)}, then decide"
            )
        if "rpi-implement" in dests and plan:
            reworked = changes is not None and changes.sort_key() > review.sort_key()
            note = " (the changes record is newer than the review; confirm the routed work)"
            cmds.append(f"/rpi-implement plan={plan.rel}" + (note if reworked else ""))
        if "rpi-plan" in dests:
            cmds.append(f"/rpi-plan task={slug}")
        if "rpi-research" in dests:
            cmds.append(f"/rpi-research topic={slug}")
        if "follow-up" in dests:
            cmds.append("plan the follow-up items as a separate task")
        if rm["final_pending"]:
            cmds.insert(0, f"record the final outcome in Current Disposition of {review.rel}")
        return "; ".join(cmds) or "none recorded"

    # ---- finalize
    def finalize(self) -> None:
        """Sort findings by severity, task, artifact, and rule, and number them."""
        self.findings.sort(
            key=lambda f: (SEVERITY_ORDER[f.severity], str(f.slug or ""), f.artifact, f.rule)
        )
        for n, f in enumerate(self.findings, 1):
            f.num = n
