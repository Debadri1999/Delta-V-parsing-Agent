"""Parser agent: extracts one DDS section (or one command) from planned FHX source.

Agentic behaviours:
* **Self-directed retrieval** - the model may answer ``need_more_context`` once; the
  requested terms are searched in the FHX index and appended to the source.
* **Schema repair** - rows that fail validation are sent back with the errors.
* **Revision** - on later iterations it receives its previous draft plus guardrail
  and critic findings and must fix them.
* **Map/merge** - source too large for one prompt is split into batches whose rows are merged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, ValidationError

from dds_agent.agents import prompts
from dds_agent.config import Settings
from dds_agent.fhx.source import SourceBundle
from dds_agent.knowledge.store import KnowledgeStore
from dds_agent.llm.client import LLMClient, LLMResponseError, estimate_tokens
from dds_agent.schema.dds import CommandSequence, SectionSpec, TableOutput, TextOutput

logger = logging.getLogger(__name__)

Retriever = Callable[[list[str]], SourceBundle]


def row_schema_text(model: type[BaseModel]) -> str:
    """Render a row model as a JSON template with field descriptions."""
    parts = []
    for name, f in model.model_fields.items():
        if name == "source_refs":
            continue
        desc = f.description or name.replace("_", " ")
        typ = "int" if f.annotation is int else "string"
        parts.append(f'  "{name}": <{typ}: {desc}>')
    parts.append('  "source_refs": ["<SOURCE id>", ...]')
    return "{\n" + ",\n".join(parts) + "\n}"


@dataclass
class ExtractResult:
    """Parser output plus diagnostics.

    Attributes:
        output: Section payload (``TableOutput``/``TextOutput`` dump or ``CommandSequence`` dump).
        invalid_rows: Validation errors of rows that were dropped.
        lookups: Search terms the agent requested.
        source: The source actually used (after retrieval).
    """

    output: dict
    invalid_rows: list[str] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)
    source: SourceBundle | None = None


class ParserAgent:
    """Section extraction agent."""

    def __init__(self, llm: LLMClient, kb: KnowledgeStore, settings: Settings) -> None:
        self.llm = llm
        self.kb = kb
        self.settings = settings

    def extract(self, spec: SectionSpec, module: str, source: SourceBundle, *, notes: str = "",
                feedback: str = "", previous: dict | None = None, retrieve: Retriever | None = None,
                label: str | None = None) -> ExtractResult:
        """Extract a section.

        Args:
            spec: Section definition.
            module: EM class name.
            source: Planned source blocks.
            notes: Cartographer hints.
            feedback: Problems from the previous iteration (empty on the first pass).
            previous: Previous draft, revised when ``feedback`` is given.
            retrieve: Callback that turns search terms into extra source.
            label: Name used in logs/ledger (defaults to the section id).
        """
        label = label or spec.id
        rules = self.kb.rules_for(spec.id)
        exemplar = self.kb.exemplars_for(spec.id, notes or spec.title, exclude_module=module) or "(none available)"
        contract = prompts.contract_for(spec.kind, row_schema_text(spec.row_model) if spec.row_model else "")
        system = prompts.PARSER_SYSTEM.format(contract=contract, rules=rules, exemplar=exemplar)
        fb = prompts.PARSER_FEEDBACK.format(previous=json.dumps(previous, indent=1)[:20000], problems=feedback) \
            if feedback and previous else ""

        overhead = estimate_tokens(system) + estimate_tokens(fb) + 1500
        budget_chars = max(4000, (self.settings.max_prompt_tokens - overhead) * 3)
        batches = _split(source, budget_chars) if spec.kind == "table" else [source]
        if len(batches) > 1:
            logger.info("%s: source split into %d batches", label, len(batches))

        result = ExtractResult(output={}, source=source)
        merged: list[ExtractResult] = []
        for i, batch in enumerate(batches):
            merged.append(self._extract_batch(spec, module, batch, system, fb, notes, retrieve, f"{label}#{i + 1}"
                                              if len(batches) > 1 else label))
        if len(merged) == 1:
            return merged[0]
        rows: dict[str, dict] = {}
        for part in merged:
            result.invalid_rows += part.invalid_rows
            result.lookups += part.lookups
            for row in part.output.get("rows", []):
                key = str(row.get(spec.key, ""))
                if key in rows:
                    for k, v in row.items():
                        if v and not rows[key].get(k):
                            rows[key][k] = v
                else:
                    rows[key] = row
        result.output = {"rows": list(rows.values()), "note": " ".join(p.output.get("note", "") for p in merged).strip()}
        return result

    def _extract_batch(self, spec: SectionSpec, module: str, source: SourceBundle, system: str, feedback: str,
                       notes: str, retrieve: Retriever | None, label: str) -> ExtractResult:
        lookups: list[str] = []
        for _ in range(2):
            user = prompts.PARSER_USER.format(title=spec.title.format(module=module), section_id=spec.id,
                                              module=module, notes=f"Planner notes: {notes}" if notes else "",
                                              feedback=feedback, source=source.text)
            try:
                raw = self.llm.chat_json(agent=f"parser:{label}", model=self.settings.generator_model,
                                         system=system, user=user)
            except LLMResponseError as exc:
                logger.error("%s: %s", label, exc)
                return ExtractResult(output=_empty(spec), invalid_rows=[str(exc)], source=source)
            if isinstance(raw, dict) and raw.get("need_more_context") and retrieve and not lookups:
                lookups = [str(q) for q in raw["need_more_context"]][:5]
                logger.info("%s: agent requested more context: %s", label, lookups)
                extra = retrieve(lookups)
                new = [r for r in extra.refs if r not in source.refs]
                if new:
                    source = SourceBundle(refs=source.refs + new, text=source.text + "\n\n" + extra.text)
                continue
            break
        out, invalid = self._validate(spec, raw)
        if invalid:  # one repair round for schema errors
            repair = (f"{feedback}\n## Schema errors in your reply\n" + "\n".join(invalid[:20]) +
                      "\nReturn the complete corrected JSON.")
            user = prompts.PARSER_USER.format(title=spec.title.format(module=module), section_id=spec.id,
                                              module=module, notes="", feedback=repair, source=source.text)
            try:
                raw2 = self.llm.chat_json(agent=f"parser:{label}:repair", model=self.settings.generator_model,
                                          system=system, user=user)
                out2, invalid2 = self._validate(spec, raw2)
                if len(invalid2) < len(invalid):
                    out, invalid = out2, invalid2
            except LLMResponseError as exc:
                logger.warning("%s: repair failed: %s", label, exc)
        return ExtractResult(output=out, invalid_rows=invalid, lookups=lookups, source=source)

    @staticmethod
    def _validate(spec: SectionSpec, raw: object) -> tuple[dict, list[str]]:
        if not isinstance(raw, dict):
            return _empty(spec), [f"reply is a {type(raw).__name__}, expected an object"]
        if spec.kind == "text":
            try:
                return TextOutput.model_validate(raw).model_dump(), []
            except ValidationError as exc:
                return _empty(spec), [str(exc)]
        if spec.kind == "commands":
            rows_raw = raw.get("rows", []) if isinstance(raw.get("rows"), list) else []
            good, bad = _validate_rows(spec, rows_raw)
            try:
                cmd = CommandSequence.model_validate({**raw, "rows": good})
                return cmd.model_dump(), bad
            except ValidationError as exc:
                return {"command": "", "block": "", "value": -1, "rows": good}, bad + [str(exc)]
        table = TableOutput.model_validate({k: v for k, v in raw.items() if k in ("rows", "note")})
        good, bad = _validate_rows(spec, table.rows)
        return {"rows": good, "note": table.note}, bad


def _validate_rows(spec: SectionSpec, rows: list) -> tuple[list[dict], list[str]]:
    good, bad = [], []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            bad.append(f"row {i}: not an object")
            continue
        row = {k: ("" if v is None else v if isinstance(v, (list, int)) else str(v)) for k, v in row.items()}
        try:
            good.append(spec.row_model.model_validate(row).model_dump())
        except ValidationError as exc:
            bad.append(f"row {i} ({row.get(spec.key, '?')}): " + "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
    return good, bad


def _empty(spec: SectionSpec) -> dict:
    if spec.kind == "text":
        return {"text": "", "source_refs": []}
    if spec.kind == "commands":
        return {"command": "", "block": "", "value": -1, "rows": []}
    return {"rows": [], "note": ""}


def _split(bundle: SourceBundle, budget_chars: int) -> list[SourceBundle]:
    """Split a bundle into batches of whole SOURCE blocks under ``budget_chars``."""
    if len(bundle.text) <= budget_chars:
        return [bundle]
    blocks = bundle.text.split("\n\n### SOURCE ")
    blocks = [blocks[0]] + ["### SOURCE " + b for b in blocks[1:]]
    batches: list[SourceBundle] = []
    cur_text: list[str] = []
    cur_refs: list[str] = []
    size = 0
    for block, ref in zip(blocks, bundle.refs):
        if cur_text and size + len(block) > budget_chars:
            batches.append(SourceBundle(refs=cur_refs, text="\n\n".join(cur_text)))
            cur_text, cur_refs, size = [], [], 0
        cur_text.append(block[:budget_chars])
        cur_refs.append(ref)
        size += len(block)
    if cur_text:
        batches.append(SourceBundle(refs=cur_refs, text="\n\n".join(cur_text)))
    return batches
