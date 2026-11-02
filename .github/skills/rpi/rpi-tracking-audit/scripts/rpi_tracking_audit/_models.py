# Copyright (c) 2026 Microsoft Corporation. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read each RPI artifact kind into a model of the fields the rules and the board use."""

from __future__ import annotations

import re
from typing import Any

from ._discovery import Artifact
from ._markdown import (
    bullet_fields,
    field_table,
    is_placeholder_value,
    labeled_blocks,
    real_bullets,
    short,
    strip_fences_and_comments,
    table_blocks,
    table_rows,
    vocab_match,
)
from ._rules import (
    ALIGNMENT_VALUES,
    ANY_PLAN_ID,
    CHECKBOX,
    CRITIQUE_EXECUTION,
    DESTINATIONS,
    EVIDENCE_CELL,
    EVIDENCE_CITE,
    EVIDENCE_DEF,
    EVIDENCE_LINE,
    EXECUTION,
    ID_HEADING,
    OUTCOMES,
    PC_ID,
    PHASE_ID,
    PLAN_READINESS,
    RD_ID,
    REQ_CITE,
    REQ_DEF,
    RESEARCH_DISPOSITIONS,
    REVIEW_EXECUTION,
    RV_ID,
    STARTED,
    TASK_ID,
    TASK_ID_IN_TEXT,
    VERDICTS,
)

Model = dict[str, Any]


def _marker_before(
    doc_lines: list[str], i: int, marker_lines: dict[int, tuple[str, str]]
) -> tuple[str, str] | None:
    """The marker on the nearest non-blank line before a heading, within two lines."""
    j = i - 1
    while j >= 0 and doc_lines[j].strip() == "" and i - j <= 2:
        j -= 1
    return marker_lines.get(j) if j >= 0 else None


def model_plan(art: Artifact) -> Model:
    """Phases, tasks, markers, requirements, readiness, critique state, and decisions."""
    doc = art.doc
    m = art.model
    meta = bullet_fields(doc.section("Task Metadata"))
    m["task_id"] = meta.get("Task ID", "")
    m["task_slug"] = meta.get("Task slug", "")
    m["plan_date"] = meta.get("Plan date", "")
    phases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    marker_lines = {i: (k, mid) for k, mid, i in doc.markers if k in ("phase", "task")}
    for _, _, i in doc.headings:
        hm = ID_HEADING.match(doc.lines[i])
        if not hm:
            continue
        hid = hm.group(3)
        if not (PHASE_ID.match(hid) or TASK_ID.match(hid)):
            continue
        blocks = labeled_blocks(doc.own_body_lines(i))
        entry: dict[str, Any] = {
            "id": hid,
            "line": i,
            "checked": (hm.group(2) or " ").lower() == "x",
            "has_box": hm.group(2) is not None,
            "marker": _marker_before(doc.lines, i, marker_lines),
            "blocks": blocks,
            "title": hm.string[hm.end() :].strip(" :-"),
        }
        if PHASE_ID.match(hid):
            entry["tasks"] = []
            phases.append(entry)
            continue
        entry["phase"] = hid.split("-")[0]
        deps = TASK_ID_IN_TEXT.findall(blocks.get("Dependencies", ""))
        entry["deps"] = [d for d in deps if d != hid]
        encl = None
        for ph in phases:
            if ph["line"] < i:
                encl = ph
        entry["enclosing"] = encl["id"] if encl else None
        if encl is not None:
            encl["tasks"].append(hid)
        tasks.append(entry)
    m["phases"] = phases
    m["tasks"] = tasks
    # markers not followed by an ID heading; a marker before a heading with a different
    # plan ID is reported from the heading side
    orphan_markers = []
    for k, mid, i in doc.markers:
        if k not in ("phase", "task"):
            continue
        j = i + 1
        while j < len(doc.lines) and doc.lines[j].strip() == "" and j - i <= 2:
            j += 1
        if not (j < len(doc.lines) and ID_HEADING.match(doc.lines[j])):
            orphan_markers.append((mid, i))
    m["orphan_markers"] = orphan_markers
    reqs_text = doc.section("Functional Requirements") + "\n"
    reqs_text += doc.section("Non-Functional Requirements")
    m["req_defined"] = [f"{a}-{b}" for a, b in REQ_DEF.findall(reqs_text)]
    cited: set[str] = set()
    for t in tasks:
        cited.update(REQ_CITE.findall(t["blocks"].get("Requirements", "")))
    m["req_cited"] = sorted(cited)
    ready_tbl = field_table(doc.section("Planning Readiness and Next Step"))
    m["readiness_raw"] = ready_tbl.get("Planning execution and readiness", "")
    # the row reads "<execution> and <readiness>"; Blocked belongs to both vocabularies
    readiness_part = re.split(r"\band\b", m["readiness_raw"], maxsplit=1)[-1]
    m["readiness"] = vocab_match(readiness_part, PLAN_READINESS)
    m["latest_critique_raw"] = ready_tbl.get("Latest critique", "")
    m["blockers_raw"] = ready_tbl.get("Blockers", "")
    m["next_action_raw"] = ready_tbl.get("Next action", "")
    m["continuation_owner"] = ready_tbl.get("Continuation owner", "")
    m["relevant_research_raw"] = ready_tbl.get("Relevant research", "")
    cd = doc.section("Critique Disposition")
    cdf = bullet_fields(cd)
    m["critique_setting"] = cdf.get("Critique setting and provenance", "")
    m["critique_status"] = cdf.get("Critique status", "")
    m["critique_latest"] = cdf.get("Latest critique and verdict", "")
    disposed: set[str] = set()
    for cells in table_rows(cd):
        if cells and PC_ID.search(cells[0]):
            disposed.update(PC_ID.findall(cells[0]))
    m["pc_disposed"] = sorted(disposed)
    claims = " ".join(
        [
            m["critique_setting"],
            m["critique_status"],
            m["critique_latest"],
            m["latest_critique_raw"],
        ]
    )
    latest = m["critique_latest"] + " " + m["latest_critique_raw"]
    # paths and slugs such as skip-link-a11y are not a recorded skip
    claim_words = re.sub(r"\S*[/\\]\S*|\S+\.md\b", " ", claims)
    m["critique_skipped"] = re.search(r"\bskip(?:s|ped)?\b(?!-)", claim_words, re.I) is not None
    m["critique_verdict"] = vocab_match(latest, ("Pass", "Revise", "Blocked"), strip_paths=True)
    m["critique_path_claimed"] = re.search(r"plan-critique(?:-\d+)?\.md", latest) is not None
    open_decisions = []
    open_status = re.compile(r"\b(proposed|unresolved|deferred|input requested)\b", re.I)
    for cells in table_rows(doc.section("User Decisions and Requirements")):
        if len(cells) >= 3 and open_status.search(cells[2]) and not is_placeholder_value(cells[1]):
            open_decisions.append((cells[0], short(cells[1], 60), cells[2]))
    m["open_decisions"] = open_decisions
    m["follow_ups"] = real_bullets(doc.section("Follow-Up Items"))
    m["h2"] = doc.h2_titles()
    return m


def model_critique(art: Artifact) -> Model:
    """Execution, verdict, findings, and the follow-up reconciliation."""
    doc = art.doc
    m = art.model
    meta = bullet_fields(doc.section("Metadata"))
    m["task_id"] = meta.get("Task ID", "")
    m["plan_ref"] = meta.get("Plan", "")
    m["execution_raw"] = meta.get("Critique execution", "")
    m["execution"] = vocab_match(m["execution_raw"], CRITIQUE_EXECUTION)
    m["started"] = m["execution"] == STARTED
    m["type"] = meta.get("Critique type", "")
    m["depth"] = meta.get("Critique depth", "")
    m["verdict_raw"] = bullet_fields(doc.section("Verdict")).get("Verdict", "")
    m["verdict"] = vocab_match(m["verdict_raw"], VERDICTS, strip_paths=True)
    ids_h, sev = [], {}
    for _, _, i in doc.headings:
        hm = ID_HEADING.match(doc.lines[i])
        if hm and hm.group(3).startswith("PC-"):
            ids_h.append(hm.group(3))
            sev[hm.group(3)] = (hm.group(4) or "").capitalize()
    m["finding_ids"] = ids_h
    m["marker_ids"] = [mid for k, mid, _ in doc.markers if k == "critique"]
    m["severities"] = sev
    m["has_reconciliation"] = doc.has_section("Earlier Finding Reconciliation")
    m["next_action"] = bullet_fields(doc.section("Recommended Next Action"))
    m["h2"] = doc.h2_titles()
    return m


def model_changes(art: Artifact) -> Model:
    """Execution status, scope, completed entries, validation, alignment, and blockers."""
    doc = art.doc
    m = art.model
    meta = bullet_fields(doc.section("Metadata"))
    m["task_id"] = meta.get("Task ID", "")
    m["plan_ref"] = meta.get("Related plan", "")
    es = bullet_fields(doc.section("Execution Status"))
    m["status_raw"] = es.get("Status", "")
    m["status"] = vocab_match(m["status_raw"], EXECUTION)
    m["scope_raw"] = es.get("Declared invocation scope", "")
    m["full_plan_scope"] = re.search(r"full[\s-]*plan", m["scope_raw"], re.I) is not None
    m["completed_markers"] = sorted(set(ANY_PLAN_ID.findall(es.get("Completed scope markers", ""))))
    remaining = es.get("All remaining active-plan markers", "")
    m["remaining_markers"] = sorted(set(ANY_PLAN_ID.findall(remaining)))
    entries = []
    for title, body, i in doc.subsection_bodies("Completed Work", 3):
        f = bullet_fields(body)
        related = sorted(set(ANY_PLAN_ID.findall(f.get("Related phase or task", ""))))
        entries.append(
            {"title": title, "related": related, "validation": f.get("Validation", ""), "line": i}
        )
    m["entries"] = entries
    m["entry_markers"] = sorted({r for e in entries for r in e["related"]})
    m["all_mentioned"] = sorted(
        set(m["completed_markers"]) | set(m["entry_markers"]) | set(m["remaining_markers"])
    )
    m["validation_rows"] = [c for c in table_rows(doc.section("Validation Record")) if len(c) >= 3]
    m["alignment_values"] = [
        vocab_match(bullet_fields(body).get("Alignment", ""), ALIGNMENT_VALUES)
        for _, body, _ in doc.subsection_bodies("Business Alignment", 3)
    ]
    pr = bullet_fields(doc.section("Pre-Review Reconciliation"))
    m["review_readiness_raw"] = pr.get("Review readiness", "")
    m["review_ready"] = re.match(r"^\s*ready\b", m["review_readiness_raw"], re.I) is not None
    m["blockers"] = real_bullets(doc.section("Blockers"))
    m["remaining_work"] = [
        b for b in real_bullets(doc.section("Remaining Work")) if not b.startswith("Canonical")
    ]
    m["h2"] = doc.h2_titles()
    return m


def model_review(art: Artifact) -> Model:
    """Execution, outcomes, findings with proposed and final routes, and decisions."""
    doc = art.doc
    m = art.model
    summ = bullet_fields(doc.section("Executive Summary"))
    rrf = bullet_fields(doc.section("Review Record"))
    m["execution_raw"] = summ.get("Review execution", "")
    m["execution"] = vocab_match(m["execution_raw"], EXECUTION)
    m["record_execution"] = vocab_match(rrf.get("Review execution", ""), REVIEW_EXECUTION)
    # the record is saved while execution stays `started`; the summary fields follow later
    m["started"] = m["record_execution"] == STARTED and m["execution"] is None
    m["outcome_raw"] = summ.get("Assessed outcome", "")
    m["outcome"] = vocab_match(m["outcome_raw"], OUTCOMES)
    findings = []
    for title, body, i in doc.subsection_bodies("Findings and Proposed Routes", 3):
        hm = ID_HEADING.match(doc.lines[i])
        if not hm or not hm.group(3).startswith("RV-"):
            continue
        dest_raw = bullet_fields(body).get("Proposed destination", "")
        findings.append(
            {
                "id": hm.group(3),
                "severity": (hm.group(4) or "").capitalize(),
                "destination": vocab_match(dest_raw, DESTINATIONS),
                "destination_raw": dest_raw,
                "title": short(title.split(":", 1)[-1], 60),
                "line": i,
            }
        )
    m["findings"] = findings
    m["marker_ids"] = [mid for k, mid, _ in doc.markers if k == "review"]
    decided: set[str] = set()
    decisions: dict[str, str] = {}
    final_dests: dict[str, str | None] = {}
    pdr = doc.section("Parent Decision Record")
    for header, rows in table_blocks(pdr):
        fin_col = next(
            (i for i, h in enumerate(header) if h.strip().lower() == "final destination"), None
        )
        for cells in rows:
            if len(cells) < 4 or not RD_ID.search(cells[0]):
                continue
            for rv in RV_ID.findall(cells[1]):
                decided.add(rv)
                decisions[rv] = cells[3]
                # the latest event governs; a recorded route replaces the proposal, `none` drops it
                if fin_col is not None and fin_col < len(cells):
                    cell = cells[fin_col]
                    if not is_placeholder_value(cell):
                        route = vocab_match(cell, DESTINATIONS)
                        none = re.search(r"\bnone\b", cell, re.I)
                        final_dests[rv] = route or ("none" if none else None)
    m["decided"] = sorted(decided)
    m["decisions"] = decisions
    for f in findings:
        fd = final_dests.get(f["id"])
        f["final_destination"] = fd
        f["route"] = f["destination"] if fd is None else (None if fd == "none" else fd)
    cur = bullet_fields(pdr)
    m["final_outcome_raw"] = cur.get("Final outcome", "")
    m["final_outcome"] = vocab_match(m["final_outcome_raw"], OUTCOMES)
    raw = m["final_outcome_raw"]
    m["final_pending"] = (
        m["final_outcome"] is None
        or re.match(r"[\s`*_]*pending\b", raw, re.I) is not None
        or (
            re.search(r"\bpending\b", raw, re.I) is not None
            and re.search(r"\b(?:nothing|none|no)\b[^.;]{0,30}\bpending\b", raw, re.I) is None
        )
    )
    m["task_id"] = rrf.get("Task ID", "")
    m["plan_ref"] = rrf.get("Plan", "")
    m["changes_ref"] = rrf.get("Changes", "")
    scope_raw = rrf.get("Review scope", "")
    m["scope_raw"] = scope_raw
    m["bounded_scope"] = ANY_PLAN_ID.search(scope_raw) is not None and not re.search(
        r"\bfull\b", scope_raw, re.I
    )
    m["h2"] = doc.h2_titles()
    return m


def model_research(art: Artifact) -> Model:
    """Readiness, disposition, and evidence IDs defined and cited."""
    doc = art.doc
    m = art.model
    tbl = field_table(doc.section("Planning Readiness and Next Step"))
    m["readiness_raw"] = tbl.get("Planning Readiness", "")
    m["readiness"] = vocab_match(m["readiness_raw"], PLAN_READINESS)
    m["disposition_raw"] = tbl.get("Research disposition", "")
    m["disposition"] = vocab_match(m["disposition_raw"], RESEARCH_DISPOSITIONS)
    m["next_action_raw"] = tbl.get("Next action", "")
    m["blockers_raw"] = tbl.get("Blockers", "")
    m["status_raw"] = bullet_fields(doc.section("Executive Summary")).get("Research status", "")
    m["evidence_defined"] = sorted(set(EVIDENCE_DEF.findall(doc.section("Research Record"))))
    user_facing = "\n".join(
        doc.section(name)
        for name in (
            "Findings",
            "Recommendation and Alternatives",
            "Decisions and Feedback",
            "What You May Not Know",
        )
    )
    # cite only where the template cites evidence, so prose such as "C4 model" is not an ID
    text = strip_fences_and_comments(user_facing)
    cited = {
        c
        for line in text.split("\n")
        if EVIDENCE_LINE.match(line)
        for c in EVIDENCE_CITE.findall(line)
    }
    for cells in table_rows(text):
        for cell in cells:
            if EVIDENCE_CELL.match(cell.strip("` ")):
                cited.update(EVIDENCE_CITE.findall(cell))
    m["evidence_cited"] = sorted(cited)
    m["h2"] = doc.h2_titles()
    return m


def model_legacy(art: Artifact) -> Model:
    """Checklist progress and title of a retired-format file."""
    m = art.model
    boxes = CHECKBOX.findall(art.text)
    m["steps_total"] = len(boxes)
    m["steps_checked"] = sum(1 for b in boxes if b.lower() == "x")
    m["title"] = art.doc.headings[0][1] if art.doc.headings else ""
    return m
