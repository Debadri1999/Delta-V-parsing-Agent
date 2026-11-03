"""Orchestrator: plan -> (parse -> guard -> critique -> revise)* -> release gate -> PDF.

For every DDS section (and every command sequence) the orchestrator runs a bounded
loop:

    parser drafts  ->  guardrails verify  ->  critic scores  ->  pass?  ->  done
          ^                                                       | no
          +------------------ feedback (errors + issues) ---------+

The best draft (no guardrail errors first, then highest score) is kept. After all
sections, cross-section guardrails run and the release gate decides whether the PDF
is issued as RELEASED FOR REVIEW or as DRAFT with the open findings listed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from dds_agent.agents.cartographer import Cartographer, Plan
from dds_agent.agents.critic import CriticAgent, Review
from dds_agent.agents.parser import ParserAgent
from dds_agent.config import Settings
from dds_agent.fhx.source import FhxSource, Selector, SourceBundle
from dds_agent.guardrails.checks import GuardrailContext, GuardrailReport, check_command, check_document, check_section
from dds_agent.knowledge.store import KnowledgeStore
from dds_agent.llm.client import LLMClient
from dds_agent.render.pdf import render_pdf
from dds_agent.schema.dds import SECTION_BY_ID, SECTIONS, CommandSequence, DdsDocument, SectionQA, SectionSpec
from dds_agent.tracing import Tracer, timing_markdown

logger = logging.getLogger(__name__)

GUARDRAIL_SCORE_CAP = 6.0


@dataclass
class LoopResult:
    output: dict
    qa: SectionQA


@dataclass
class RunResult:
    """Artifacts of one run."""

    document: DdsDocument
    pdf_path: Path
    json_path: Path
    qa_path: Path
    timing_path: Path


class Orchestrator:
    """Runs the multi-agent pipeline for one FHX file."""

    def __init__(self, settings: Settings, llm: LLMClient | None = None, kb: KnowledgeStore | None = None,
                 workers: int = 4) -> None:
        self.settings = settings
        self.llm = llm or LLMClient(settings)
        start = time.perf_counter()
        self.kb = kb or KnowledgeStore.load(settings.exemplar_dir)
        self._kb_load_s = time.perf_counter() - start
        self.cartographer = Cartographer(self.llm, self.kb, settings)
        self.parser = ParserAgent(self.llm, self.kb, settings)
        self.critic = CriticAgent(self.llm, self.kb, settings)
        self.workers = workers
        self.tracer = Tracer()

    # ------------------------------------------------------------------ public

    def run(self, fhx_path: Path, out_dir: Path | None = None, sections: list[str] | None = None) -> RunResult:
        """Generate the DDS for ``fhx_path``.

        Args:
            fhx_path: Input FHX export.
            out_dir: Output folder (default ``<output_dir>/<module>``).
            sections: Optional subset of section ids (for quick iterations).
        """
        self.tracer = tr = Tracer()
        tr.record("knowledge_base_load", self._kb_load_s, start_s=0.0)
        self._kb_load_s = 0.0  # only the first run of this orchestrator pays for it
        with tr.span("fhx_index"):
            source = FhxSource.load(fhx_path)
        with tr.span("cartographer_plan"):
            plan = self.cartographer.plan(source)
        ctx = GuardrailContext(source, plan.main_module, plan.command_set)
        module = plan.main_module
        wanted = [s for s in SECTIONS if sections is None or s.id in sections]

        doc = DdsDocument(module=module, source_file=fhx_path.name,
                          description=str(source.top[module].get("DESCRIPTION", "")) if module in source.top else "",
                          models={"generator": self.settings.generator_model, "critic": self.settings.critic_model},
                          generated_at=datetime.now().isoformat(timespec="seconds"))

        jobs: list[tuple[str, Callable[[], LoopResult]]] = []
        for spec in wanted:
            if spec.kind == "commands":
                for cmd in plan.commands:
                    jobs.append((f"command_sequences:{cmd.block}",
                                 lambda c=cmd, s=spec: self._command_loop(s, c.block, c.definition, plan, source, ctx)))
            else:
                jobs.append((spec.id, lambda s=spec: self._section_loop(s, plan, source, ctx)))

        with tr.span("sections_parallel"), ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {name: pool.submit(self._guarded, name, fn) for name, fn in jobs}
            results = {name: f.result() for name, f in futures.items()}

        for name, res in results.items():
            if name.startswith("command_sequences:"):
                doc.commands.append(CommandSequence.model_validate(res.output))
            else:
                doc.sections[name] = res.output
            doc.qa.append(res.qa)
        doc.commands.sort(key=lambda c: c.block)

        with tr.span("cross_section_guardrails"):
            cross = check_document(doc.sections, ctx)
        if cross.errors or cross.warnings:
            doc.qa.append(SectionQA(section_id="document", score=10.0 if cross.ok else GUARDRAIL_SCORE_CAP,
                                    passed=cross.ok, guardrail_errors=cross.errors, guardrail_warnings=cross.warnings))
        for w in plan.warnings:
            logger.info("plan warning recorded: %s", w)
        with tr.span("release_gate"):
            doc.released, doc.release_reason = self._release_gate(doc)
        return self._write(doc, plan, out_dir or self.settings.output_dir / _slug(module))

    # ------------------------------------------------------------------- loops

    @staticmethod
    def _guarded(name: str, fn: Callable[[], LoopResult]) -> LoopResult:
        """Run one section job; a crash becomes a failed QA record instead of aborting the run."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - isolate any agent/transport failure
            logger.exception("%s failed", name)
            output: dict = {"rows": [], "note": "Section could not be generated in this run."}
            if name.startswith("command_sequences:"):
                block = name.split(":", 1)[1]
                output = {"command": block, "block": block, "value": int(re.sub(r"\D", "", block) or -1), "rows": []}
            elif SECTION_BY_ID.get(name) and SECTION_BY_ID[name].kind == "text":
                output = {"text": "", "source_refs": []}
            return LoopResult(output, SectionQA(section_id=name, guardrail_errors=[f"generation failed: {exc}"]))

    def _bundle(self, spec: SectionSpec, plan: Plan, source: FhxSource) -> tuple[SourceBundle, str]:
        sp = plan.sections.get(spec.id)
        nodes, seen = [], set()
        for sel in sp.selectors if sp else []:
            for n in source.select(sel):
                if n.ref not in seen:
                    seen.add(n.ref)
                    nodes.append(n)
        bundle = SourceBundle.from_nodes(nodes)
        if sp and sp.queries:
            extra = self._retrieve(source, sp.queries, exclude=set(bundle.refs), scope=None)
            if extra.refs:
                bundle = SourceBundle(refs=bundle.refs + extra.refs, text=bundle.text + "\n\n" + extra.text)
        return bundle, sp.notes if sp else ""

    def _retrieve(self, source: FhxSource, queries: list[str], exclude: set[str], scope: str | None,
                  per_query: int = 3) -> SourceBundle:
        nodes = []
        for q in queries:
            for hit in source.search(q, per_query, scope=scope):
                if hit.item.id not in exclude:
                    exclude.add(hit.item.id)
                    nodes.append(hit.item)
        return SourceBundle(refs=[c.id for c in nodes],
                            text="\n\n".join(f"### SOURCE [{c.id}]\n{c.text}" for c in nodes))

    def _section_loop(self, spec: SectionSpec, plan: Plan, source: FhxSource, ctx: GuardrailContext) -> LoopResult:
        with self.tracer.span("source_selection", spec.id):
            bundle, notes = self._bundle(spec, plan, source)
        retrieve = lambda qs: self._retrieve(source, qs, set(bundle.refs), None)  # noqa: E731
        return self._loop(spec, spec.id, plan.main_module, bundle, notes, retrieve, source,
                          lambda out, src, invalid: check_section(spec, out, src, ctx, invalid))

    def _command_loop(self, spec: SectionSpec, block: str, definition: str, plan: Plan, source: FhxSource,
                      ctx: GuardrailContext) -> LoopResult:
        label = f"command_sequences:{block}"
        with self.tracer.span("source_selection", label):
            nodes = source.select(Selector(scope=plan.main_module, keyword="FUNCTION_BLOCK",
                                           name_regex=re.escape(block), follow_references=True))
            if plan.command_set:
                nodes += source.select(Selector(scope=plan.command_set, scope_is_target=True, keyword="ENTRY"))
            bundle = SourceBundle.from_nodes(nodes)
        notes = (f"Document command block {block} (definition {definition}). The command name comes from named set "
                 f"{plan.command_set or '(see source)'}.")
        retrieve = lambda qs: self._retrieve(source, qs, set(bundle.refs), None)  # noqa: E731
        return self._loop(spec, label, plan.main_module, bundle, notes, retrieve, source,
                          lambda out, src, invalid: check_command(spec, out, src, ctx, block, definition, invalid))

    def _loop(self, spec: SectionSpec, label: str, module: str, bundle: SourceBundle, notes: str, retrieve,
              source: FhxSource, guard: Callable[[dict, SourceBundle, list[str]], GuardrailReport]) -> LoopResult:
        best: tuple[tuple[bool, float], dict, SectionQA] | None = None
        feedback, previous = "", None
        for it in range(1, self.settings.max_iterations + 1):
            with self.tracer.span("parser", label, it):
                res = self.parser.extract(spec, module, bundle, notes=notes, feedback=feedback, previous=previous,
                                          retrieve=retrieve, label=label)
            src = res.source or bundle
            with self.tracer.span("guardrails", label, it):
                rep = guard(res.output, src, res.invalid_rows)
            with self.tracer.span("critic", label, it):
                review = self.critic.review(spec, module, res.output, src, rep, label=label)
            score = review.score if rep.ok else min(review.score, GUARDRAIL_SCORE_CAP)
            passed = rep.ok and not review.failed and score >= self.settings.pass_score
            qa = SectionQA(section_id=label, score=score, iterations=it, passed=passed,
                           guardrail_errors=rep.errors, guardrail_warnings=rep.warnings,
                           open_issues=[i.render() for i in review.issues], critic_summary=review.summary)
            rank = (rep.ok, score)
            if best is None or rank > best[0]:
                best = (rank, res.output, qa)
            logger.info("%s: iteration %d score=%.1f guardrail_errors=%d issues=%d%s", label, it, score,
                        len(rep.errors), len(review.issues), " PASS" if passed else "")
            if passed:
                break
            feedback = _feedback(rep, review)
            previous = res.output
            with self.tracer.span("guardrail_fetch", label, it):
                bundle = _augment(src, rep, source, label)
        assert best is not None
        best[2].iterations = it
        return LoopResult(best[1], best[2])

    # ------------------------------------------------------------------- gate

    def _release_gate(self, doc: DdsDocument) -> tuple[bool, str]:
        scored = [q for q in doc.qa if q.section_id != "document"]
        if not scored:
            return False, "no sections were generated"
        blocking = [q.section_id for q in doc.qa if q.guardrail_errors]
        mean = sum(q.score for q in scored) / len(scored)
        low = [q.section_id for q in scored if q.score < self.settings.release_min_section]
        reasons = []
        if blocking:
            reasons.append(f"guardrail errors in {', '.join(blocking)}")
        if mean < self.settings.release_min_mean:
            reasons.append(f"mean score {mean:.1f} < {self.settings.release_min_mean}")
        if low:
            reasons.append(f"sections below {self.settings.release_min_section}: {', '.join(low)}")
        if reasons:
            return False, "; ".join(reasons)
        return True, f"all guardrails passed; mean critic score {mean:.1f}"

    # ------------------------------------------------------------------ output

    def _write(self, doc: DdsDocument, plan: Plan, out_dir: Path) -> RunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = _slug(doc.module)
        json_path = out_dir / f"{stem}_dds.json"
        qa_path = out_dir / f"{stem}_qa.json"
        pdf_path = out_dir / f"{stem}_DDS.pdf"
        timing_path = out_dir / f"{stem}_timing.md"
        with self.tracer.span("write_outputs"):
            json_path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
        with self.tracer.span("render_pdf"):
            render_pdf(doc, pdf_path)
        timing = self.tracer.summary(self.llm.ledger.calls)
        qa_path.write_text(json.dumps({
            "module": doc.module, "released": doc.released, "release_reason": doc.release_reason,
            "sections": [q.model_dump() for q in doc.qa], "plan": plan.model_dump(),
            "llm_usage": self.llm.ledger.summary(), "timing": timing}, indent=2), encoding="utf-8")
        timing_path.write_text(timing_markdown(doc.module, timing, self.workers), encoding="utf-8")
        logger.info("%s: %s -> %s (%.1fs)", doc.module, "RELEASED" if doc.released else "DRAFT", pdf_path,
                    timing["total_s"])
        return RunResult(doc, pdf_path, json_path, qa_path, timing_path)


def _augment(bundle: SourceBundle, rep: GuardrailReport, source: FhxSource, label: str) -> SourceBundle:
    """Add blocks the guardrails proved missing from the agent's source."""
    nodes = []
    for sel in rep.fetch:
        try:
            nodes += [n for n in source.select(sel) if n.ref not in bundle.refs and n.ref not in {x.ref for x in nodes}]
        except re.error as exc:
            logger.warning("%s: invalid fetch selector %s: %s", label, sel, exc)
    if not nodes:
        return bundle
    logger.info("%s: guardrails added %d source block(s) for the next iteration", label, len(nodes))
    extra = SourceBundle.from_nodes(nodes)
    return SourceBundle(refs=bundle.refs + extra.refs, text=bundle.text + "\n\n" + extra.text)


def _feedback(rep: GuardrailReport, review: Review) -> str:
    lines = [f"- [guardrail] {e}" for e in rep.errors]
    lines += [f"- [reviewer] {i.render()}" for i in review.issues]
    if review.failed:
        lines.append("- [reviewer] review failed; re-check the draft against the rules")
    return "\n".join(lines) or "- Improve accuracy and completeness."


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip("_")) or "module"
