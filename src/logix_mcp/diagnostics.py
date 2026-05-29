"""Static-analysis rules over an ingested Logix workspace.

The ingest quality gate measures *extraction* completeness. This module is the
complementary layer: it reasons about the *logic* to surface the problems an
engineer would look for in a review - duplicate destructive writes, dead tags,
unscheduled programs, inhibited hardware, broken aliases, and unused library
objects.

``run_diagnostics(workspace)`` returns prioritized findings. Each rule is a small
pure function over a shared :class:`_Context` so it can be unit-tested in
isolation.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import db, graph
from .workspace import read_jsonl


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

READ_ACCESS = {"read", "read_write"}
WRITE_ACCESS = {"write", "read_write"}

# Per-rule cap so a single noisy rule cannot dominate the report.
PER_RULE_LIMIT = 500


class _Context:
    """Loads the workspace surfaces once and derives the lookup structures."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = workspace
        self.xrefs = graph._xrefs_min(workspace)
        self.symbols = read_jsonl(workspace, "symbols.jsonl")
        self.tags = [row for row in self.symbols if row.get("kind") == "tag"]
        self.aois = [row for row in self.symbols if row.get("kind") == "aoi"]
        self.udts = read_jsonl(workspace, "data_types.jsonl")
        self.aoi_parameters = read_jsonl(workspace, "aoi_parameters.jsonl")
        self.modules = read_jsonl(workspace, "modules.jsonl")
        self.alarms = read_jsonl(workspace, "alarms.jsonl")
        self.routine_index = graph._routine_index(workspace)

        self.tag_by_name: dict[str, dict] = {}
        for tag in self.tags:
            self.tag_by_name.setdefault(tag.get("name"), tag)
        self.tag_names = set(self.tag_by_name)

        self.hard_writers: dict[str, set[str]] = defaultdict(set)  # access == write
        self.any_writers: dict[str, set[str]] = defaultdict(set)
        self.any_readers: dict[str, set[str]] = defaultdict(set)
        self.used_instructions: set[str] = set()
        for ref in self.xrefs:
            base = ref.get("base_symbol") or graph.base_symbol(ref.get("symbol") or "")
            routine = ref.get("routine")
            access = ref.get("access")
            if ref.get("instruction"):
                self.used_instructions.add(str(ref["instruction"]).upper())
            if not base or not routine:
                continue
            if access == "write":
                self.hard_writers[base].add(routine)
            if access in WRITE_ACCESS:
                self.any_writers[base].add(routine)
            if access in READ_ACCESS:
                self.any_readers[base].add(routine)

        self.used_data_types: set[str] = set()
        for tag in self.tags:
            if tag.get("data_type"):
                self.used_data_types.add(tag["data_type"])
        for param in self.aoi_parameters:
            if param.get("data_type"):
                self.used_data_types.add(param["data_type"])
        for udt in self.udts:
            for member in udt.get("members", []):
                if member.get("data_type"):
                    self.used_data_types.add(member["data_type"])

    def routine_label(self, routine_id: str) -> str:
        info = self.routine_index.get(routine_id)
        if not info:
            return routine_id
        program = info.get("program") or (info.get("owner") or "").split(":", 1)[-1]
        return f"{program}/{info.get('routine')}" if program else str(info.get("routine"))


def _finding(rule: str, severity: str, entity: str, title: str, detail: str, **extra) -> dict:
    return {"rule": rule, "severity": severity, "entity": entity, "title": title, "detail": detail, **extra}


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def rule_multiple_output(ctx: _Context) -> list[dict]:
    """Tags destructively written (access=write) by more than one routine."""

    findings = []
    for base, routines in ctx.hard_writers.items():
        if len(routines) < 2 or base not in ctx.tag_names:
            continue
        data_type = (ctx.tag_by_name.get(base) or {}).get("data_type")
        severity = "warning" if data_type == "BOOL" else "info"
        labels = sorted(ctx.routine_label(rid) for rid in routines)
        findings.append(
            _finding(
                "multiple_output",
                severity,
                base,
                f"'{base}' is written by {len(routines)} routines",
                "Multiple destructive writers can race or override each other; confirm this is intentional.",
                data_type=data_type,
                writer_count=len(routines),
                writers=labels[:10],
            )
        )
    findings.sort(key=lambda f: -f.get("writer_count", 0))
    return findings


def rule_written_never_read(ctx: _Context) -> list[dict]:
    """Tags written by logic but never read anywhere."""

    findings = []
    for base in sorted(ctx.any_writers):
        if base not in ctx.tag_names or base in ctx.any_readers:
            continue
        tag = ctx.tag_by_name.get(base) or {}
        if tag.get("tag_type") == "Alias" or tag.get("alias_for"):
            continue
        findings.append(
            _finding(
                "written_never_read",
                "info",
                base,
                f"'{base}' is written but never read",
                "Possible dead output, or consumed only by HMI/comms outside this project.",
                data_type=tag.get("data_type"),
            )
        )
    return findings


def rule_read_never_written(ctx: _Context) -> list[dict]:
    """Tags read by logic but never written, excluding aliases and constants."""

    findings = []
    for base in sorted(ctx.any_readers):
        if base not in ctx.tag_names or base in ctx.any_writers:
            continue
        tag = ctx.tag_by_name.get(base) or {}
        if tag.get("tag_type") == "Alias" or tag.get("alias_for"):
            continue
        if str(tag.get("constant")).lower() == "true":
            continue
        findings.append(
            _finding(
                "read_never_written",
                "info",
                base,
                f"'{base}' is read but never written",
                "May be driven externally (HMI/comms/produced tag) or left uninitialized.",
                data_type=tag.get("data_type"),
            )
        )
    return findings


def rule_broken_alias(ctx: _Context) -> list[dict]:
    """Alias tags whose target does not resolve to a known tag or module point."""

    module_names = {row.get("name") for row in ctx.modules}
    findings = []
    for tag in ctx.tags:
        target = tag.get("alias_for")
        if not target:
            continue
        target_base = graph.base_symbol(target)
        # Module-qualified references (Local:1:I.Data) carry a colon; skip those.
        if ":" in target or target_base in ctx.tag_names or target_base in module_names:
            continue
        findings.append(
            _finding(
                "broken_alias",
                "error",
                tag.get("name"),
                f"Alias '{tag.get('name')}' points to unknown '{target}'",
                "The alias target was not found among tags or modules; logic referencing it may not resolve.",
                alias_for=target,
            )
        )
    return findings


def rule_unscheduled_programs(ctx: _Context) -> list[dict]:
    """Programs that are not scheduled under any task and will not execute."""

    tree = graph.call_graph(ctx.workspace)
    findings = [
        _finding(
            "unscheduled_program",
            "warning",
            name,
            f"Program '{name}' is not scheduled in any task",
            "Unscheduled programs do not execute; remove them or add them to a task.",
        )
        for name in tree.get("unscheduled_programs", [])
    ]
    return findings


def rule_inhibited_or_faulted_modules(ctx: _Context) -> list[dict]:
    """Modules that are inhibited or report a major fault."""

    findings = []
    for module in ctx.modules:
        inhibited = str(module.get("inhibited")).lower() == "true"
        faulted = str(module.get("major_fault")).lower() == "true"
        if not inhibited and not faulted:
            continue
        states = []
        if inhibited:
            states.append("inhibited")
        if faulted:
            states.append("major fault")
        findings.append(
            _finding(
                "inhibited_or_faulted_module",
                "warning",
                module.get("name"),
                f"Module '{module.get('name')}' is {' and '.join(states)}",
                "Inhibited or faulted modules will not exchange I/O; confirm this is expected.",
                catalog_number=module.get("catalog_number"),
                inhibited=inhibited,
                major_fault=faulted,
            )
        )
    return findings


def rule_aoi_never_instantiated(ctx: _Context) -> list[dict]:
    """AOI definitions that are never called nor used as a tag data type."""

    findings = []
    for aoi in ctx.aois:
        name = aoi.get("name") or ""
        if name.upper() in ctx.used_instructions or name in ctx.used_data_types:
            continue
        findings.append(
            _finding(
                "aoi_never_instantiated",
                "info",
                name,
                f"AOI '{name}' is never instantiated",
                "Defined but unused; candidate for cleanup or a missing call.",
            )
        )
    return findings


def rule_udt_never_used(ctx: _Context) -> list[dict]:
    """User-defined types not referenced by any tag, member, or parameter."""

    findings = []
    for udt in ctx.udts:
        name = udt.get("name") or ""
        if name in ctx.used_data_types:
            continue
        findings.append(
            _finding(
                "udt_never_used",
                "info",
                name,
                f"UDT '{name}' is never used",
                "Defined but not referenced as a data type anywhere; candidate for cleanup.",
            )
        )
    return findings


def rule_alarm_tag_unknown(ctx: _Context) -> list[dict]:
    """Alarms whose backing tag is not present among the project's tags."""

    findings = []
    for alarm in ctx.alarms:
        tag_name = alarm.get("tag_name") or ""
        base = graph.base_symbol(tag_name)
        if not base or base in ctx.tag_names:
            continue
        findings.append(
            _finding(
                "alarm_tag_unknown",
                "info",
                tag_name,
                f"Alarm on '{tag_name}' has no matching tag",
                "The alarm tag was not found among controller/program tags (may be scoped or external).",
                alarm_type=alarm.get("alarm_type"),
            )
        )
    return findings


RULES = [
    rule_multiple_output,
    rule_broken_alias,
    rule_unscheduled_programs,
    rule_inhibited_or_faulted_modules,
    rule_written_never_read,
    rule_read_never_written,
    rule_aoi_never_instantiated,
    rule_udt_never_used,
    rule_alarm_tag_unknown,
]


def run_diagnostics(workspace: str | Path) -> dict:
    """Run every diagnostic rule and return prioritized findings."""

    ctx = _Context(workspace)
    findings: list[dict] = []
    true_counts: dict[str, int] = {}
    for rule in RULES:
        produced = rule(ctx)
        if produced:
            rule_name = produced[0]["rule"]
            true_counts[rule_name] = true_counts.get(rule_name, 0) + len(produced)
        # Cap each rule's contribution so one noisy rule cannot dominate; the
        # uncapped totals are reported under summary.truncated so nothing is
        # silently dropped.
        findings.extend(produced[:PER_RULE_LIMIT])

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["rule"], f["entity"] or ""))

    by_rule: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_rule[finding["rule"]] = by_rule.get(finding["rule"], 0) + 1
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1

    truncated = {rule: total for rule, total in true_counts.items() if total > by_rule.get(rule, 0)}

    return {
        "summary": {
            "total": len(findings),
            "total_uncapped": sum(true_counts.values()),
            "by_severity": by_severity,
            "by_rule": by_rule,
            "truncated": truncated,
            "per_rule_limit": PER_RULE_LIMIT,
        },
        "findings": findings,
    }


def diagnostics_markdown(result: dict) -> str:
    """Render a diagnostics result as a Markdown report."""

    summary = result.get("summary", {})
    lines = [
        "# Diagnostics",
        "",
        f"Total findings: {summary.get('total', 0)}",
        "",
        "## By severity",
        "",
    ]
    for severity in ("error", "warning", "info"):
        count = summary.get("by_severity", {}).get(severity, 0)
        lines.append(f"- {severity}: {count}")
    truncated = summary.get("truncated", {})
    lines.extend(["", "## By rule", "", "| Rule | Shown | Total |", "| --- | ---: | ---: |"])
    for rule, count in sorted(summary.get("by_rule", {}).items(), key=lambda kv: -kv[1]):
        total = truncated.get(rule, count)
        lines.append(f"| {rule} | {count} | {total} |")
    if truncated:
        capped = ", ".join(f"{rule} ({total})" for rule, total in sorted(truncated.items()))
        lines.extend(["", f"> Some rules exceed the per-rule display limit of {summary.get('per_rule_limit')}: {capped}."])

    lines.extend(["", "## Findings", "", "| Severity | Rule | Entity | Detail |", "| --- | --- | --- | --- |"])
    for finding in result.get("findings", [])[:1000]:
        detail = str(finding.get("title") or finding.get("detail") or "").replace("|", "\\|")
        lines.append(f"| {finding['severity']} | {finding['rule']} | {finding.get('entity') or ''} | {detail} |")
    return "\n".join(lines) + "\n"
