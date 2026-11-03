"""Cartographer agent: maps an unfamiliar FHX file onto the DDS sections.

This is what lets the system cope with exports whose structure differs from the
ones it was built on: instead of hard-coded paths, an LLM reads the file outline
and emits structural selectors per section. Selectors are validated against the
file; any section left without resolvable source falls back to lexical retrieval.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field, ValidationError

from dds_agent.agents import prompts
from dds_agent.config import Settings
from dds_agent.fhx.source import FhxSource, Selector
from dds_agent.knowledge.store import KnowledgeStore
from dds_agent.llm.client import LLMClient
from dds_agent.schema.dds import SECTIONS

logger = logging.getLogger(__name__)


class SectionPlan(BaseModel):
    selectors: list[Selector] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    notes: str = ""


class CommandRef(BaseModel):
    block: str
    definition: str = ""


class Plan(BaseModel):
    """Where each DDS section reads from."""

    main_module: str
    command_set: str = ""
    commands: list[CommandRef] = Field(default_factory=list)
    sections: dict[str, SectionPlan] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Cartographer:
    """Plans section sources for one FHX file."""

    REPAIR_ROUNDS = 2

    def __init__(self, llm: LLMClient, kb: KnowledgeStore, settings: Settings) -> None:
        self.llm = llm
        self.kb = kb
        self.settings = settings

    def plan(self, source: FhxSource) -> Plan:
        """Build and validate a plan for ``source``."""
        roots = source.root_blocks("MODULE_CLASS") or source.root_blocks()
        focus = roots[0] if roots else None
        section_rules = "\n\n".join(
            f"### {s.id}\n{self.kb.rules_for(s.id, include_general=False) or s.title}" for s in SECTIONS)
        user = prompts.CARTOGRAPHER_USER.format(section_rules=section_rules, outline=source.outline(focus=focus))
        plan = self._ask(user, focus)
        report, problems = self._execution_report(plan, source)
        for round_no in range(1, self.REPAIR_ROUNDS + 1):
            if not problems:
                break
            # Self-repair: show the agent what its selectors actually matched.
            logger.info("Cartographer repair round %d: %d problem(s)", round_no, problems)
            repair = (user + "\n\n## Your previous plan\n" + plan.model_dump_json(exclude={"warnings"})
                      + "\n\n" + prompts.CARTOGRAPHER_REPAIR.format(report=report))
            repaired = self._ask(repair, focus)
            new_report, new_problems = self._execution_report(repaired, source)
            if new_problems <= problems:
                plan, report, problems = repaired, new_report, new_problems
        return self._validate(plan, source, focus)

    def _ask(self, user: str, focus: str | None) -> Plan:
        raw = self.llm.chat_json(agent="cartographer", model=self.settings.generator_model,
                                 system=prompts.CARTOGRAPHER_SYSTEM, user=user)
        try:
            return Plan.model_validate(raw)
        except ValidationError as exc:
            logger.warning("Cartographer output invalid (%d errors); using a minimal plan", exc.error_count())
            return Plan(main_module=focus or "", warnings=["cartographer output failed validation"])

    def _execution_report(self, plan: Plan, source: FhxSource) -> tuple[str, int]:
        """Run every selector and describe the result; count problems worth a repair round."""
        lines, problems = [], 0
        if plan.main_module not in source.top:
            lines.append(f"main_module {plan.main_module!r} is not a top-level block")
            problems += 1
        for spec in SECTIONS:
            if spec.kind == "commands":
                continue
            sp = plan.sections.get(spec.id)
            if sp is None or not sp.selectors:
                lines.append(f"- {spec.id}: no selectors")
                problems += 1
                continue
            total, children = 0, 0
            for sel in sp.selectors:
                desc = sel.model_dump_json(exclude_defaults=True)
                try:
                    nodes = source.select(sel)
                except Exception as exc:  # invalid regex
                    lines.append(f"- {spec.id}: {desc} -> ERROR {exc}")
                    problems += 1
                    continue
                size = sum(len(n.text) for n in nodes)
                total += size
                children += sum(1 for n in nodes if ":" in n.ref.split("::", 1)[-1])
                flag = "  <-- matched nothing" if not nodes else ""
                problems += not nodes
                lines.append(f"- {spec.id}: {desc} -> {len(nodes)} nodes, {size} chars{flag}")
                if not nodes:
                    lines.append(f"    similar blocks in the file: {self._did_you_mean(sel, source)}")
            if total > 90_000:
                lines.append(f"  {spec.id} total {total} chars  <-- too large, narrow it")
                problems += 1
            if spec.kind == "table" and spec.id != "properties" and not children:
                lines.append(f"  {spec.id} selects only block headers  <-- a table section needs the child blocks "
                             f"its rows come from")
                problems += 1
        return "\n".join(lines), problems

    @staticmethod
    def _did_you_mean(sel: Selector, source: FhxSource, top_k: int = 5) -> str:
        """Chunk ids (scope::KEYWORD:NAME) lexically close to a failed selector."""
        terms = " ".join(re.findall(r"[A-Za-z_]{2,}", f"{sel.keyword} {sel.name_regex or ''} {sel.text_regex or ''}"))
        hits = source.search(terms.replace("*", ""), top_k) if terms.strip("* ") else []
        return ", ".join(h.item.id for h in hits) or "none"

    def _validate(self, plan: Plan, source: FhxSource, focus: str | None) -> Plan:
        if plan.main_module not in source.top:
            plan.warnings.append(f"planned main module {plan.main_module!r} not found; using {focus!r}")
            plan.main_module = focus or ""
        main = source.top.get(plan.main_module)
        if plan.command_set and plan.command_set not in source.top:
            plan.warnings.append(f"command set {plan.command_set!r} not found")
            plan.command_set = ""
        if main is not None:
            blocks = {c.name: c for c in main.children if c.keyword == "FUNCTION_BLOCK"}
            valid = [c for c in plan.commands if c.block in blocks]
            if len(valid) != len(plan.commands):
                plan.warnings.append("dropped planned commands that are not blocks of the main module")
            if not valid:  # fall back to the naming convention
                valid = [CommandRef(block=n, definition=str(b.get("DEFINITION")))
                         for n, b in blocks.items() if re.fullmatch(r"COMMAND_\d+", n)]
            for c in valid:
                c.definition = str(blocks[c.block].get("DEFINITION")) or c.definition
            plan.commands = sorted(valid, key=lambda c: c.block)
        for spec in SECTIONS:
            if spec.kind == "commands":
                continue
            sp = plan.sections.setdefault(spec.id, SectionPlan())
            good: list[Selector] = []
            for sel in sp.selectors:
                try:
                    if source.select(sel, max_nodes=1):
                        good.append(sel)
                    else:
                        plan.warnings.append(f"{spec.id}: selector matched nothing: {sel.model_dump(exclude_defaults=True)}")
                except Exception as exc:  # bad regex or scope
                    plan.warnings.append(f"{spec.id}: invalid selector {sel}: {exc}")
            sp.selectors = good
            if not good:
                plan.warnings.append(f"{spec.id}: no valid selectors; header + retrieval fallback")
                sp.selectors = [Selector(scope=plan.main_module, scope_is_target=True)]
                sp.queries = sp.queries or [spec.title]
        for w in plan.warnings:
            logger.warning("plan: %s", w)
        logger.info("Plan: module=%s command_set=%s commands=%d", plan.main_module, plan.command_set, len(plan.commands))
        return plan
