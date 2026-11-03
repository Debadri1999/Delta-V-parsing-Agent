"""Run tracing: timed spans for every pipeline step, aggregated into a timing report.

Section jobs run in parallel, so two views are reported:

* **phases** - wall-clock time of the sequential stages of a run (they add up to the total);
* **steps** - time spent in each step inside the section jobs, summed across jobs
  (these overlap in time and can exceed the wall-clock of the parallel phase).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Iterator

PHASES = ("knowledge_base_load", "fhx_index", "cartographer_plan", "sections_parallel",
          "cross_section_guardrails", "release_gate", "write_outputs", "render_pdf")
JOB_STEPS = ("source_selection", "parser", "guardrails", "critic", "guardrail_fetch")


@dataclass
class Span:
    """One timed step."""

    step: str
    job: str
    start_s: float
    seconds: float
    iteration: int = 0


class Tracer:
    """Thread-safe collector of :class:`Span` records relative to the tracer's creation time."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self.spans: list[Span] = []

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def record(self, step: str, seconds: float, job: str = "run", start_s: float | None = None,
               iteration: int = 0) -> None:
        """Add a span measured elsewhere."""
        with self._lock:
            self.spans.append(Span(step, job, self.elapsed() - seconds if start_s is None else start_s,
                                   seconds, iteration))

    @contextmanager
    def span(self, step: str, job: str = "run", iteration: int = 0) -> Iterator[None]:
        """Time the enclosed block as ``step`` of ``job``."""
        start = self.elapsed()
        try:
            yield
        finally:
            self.record(step, self.elapsed() - start, job, start, iteration)

    def summary(self, llm_calls: list[Any] = ()) -> dict[str, Any]:
        """Aggregate spans (and optional LLM call records) into the timing report data."""
        with self._lock:
            spans = list(self.spans)
        run = [s for s in spans if s.job == "run"]
        phases = {p: round(sum(s.seconds for s in run if s.step == p), 3) for p in PHASES}
        total = sum(phases.values())

        steps: dict[str, dict[str, float]] = {}
        jobs: dict[str, dict[str, Any]] = defaultdict(lambda: {"iterations": 0, "start_s": None, "end_s": 0.0})
        for s in spans:
            if s.job == "run":
                continue
            st = steps.setdefault(s.step, {"count": 0, "total_s": 0.0, "max_s": 0.0})
            st["count"] += 1
            st["total_s"] += s.seconds
            st["max_s"] = max(st["max_s"], s.seconds)
            j = jobs[s.job]
            j[s.step] = j.get(s.step, 0.0) + s.seconds
            j["iterations"] = max(j["iterations"], s.iteration)
            j["start_s"] = s.start_s if j["start_s"] is None else min(j["start_s"], s.start_s)
            j["end_s"] = max(j["end_s"], s.start_s + s.seconds)
        for st in steps.values():
            st["mean_s"] = st["total_s"] / st["count"]
        for j in jobs.values():
            j["wall_s"] = j["end_s"] - (j["start_s"] or 0.0)

        llm: dict[str, dict[str, float]] = {}
        for c in llm_calls:
            role = c.agent.split(":", 1)[0]
            r = llm.setdefault(f"{role} ({c.model})", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                       "seconds": 0.0, "max_s": 0.0, "rate_limit_wait_s": 0.0})
            r["calls"] += 1
            r["prompt_tokens"] += c.prompt_tokens
            r["completion_tokens"] += c.completion_tokens
            r["seconds"] += c.seconds
            r["max_s"] = max(r["max_s"], c.seconds)
            r["rate_limit_wait_s"] += getattr(c, "wait_s", 0.0)
        return {"total_s": round(total, 3), "phases": phases, "steps": steps, "jobs": dict(jobs), "llm": llm,
                "spans": [asdict(s) for s in spans]}


def _fmt(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m)}m {s:04.1f}s" if m else f"{s:.1f}s"


def timing_markdown(module: str, t: dict[str, Any], workers: int) -> str:
    """Render :meth:`Tracer.summary` output as a Markdown report."""
    total = t["total_s"] or 1e-9
    out = [f"# Timing trace: {module}", "",
           f"**Total wall-clock: {_fmt(t['total_s'])}** ({t['total_s']:.1f} s)", "",
           "## Pipeline phases (wall-clock, sequential)", "",
           "| Phase | Time | Share |", "|---|---:|---:|"]
    for phase, sec in t["phases"].items():
        label = f"{phase} ({len(t['jobs'])} jobs, {workers} workers)" if phase == "sections_parallel" else phase
        out.append(f"| {label} | {_fmt(sec)} | {100 * sec / total:.1f}% |")

    out += ["", "## Steps inside section jobs (summed across parallel jobs)", "",
            "| Step | Count | Total | Mean | Max |", "|---|---:|---:|---:|---:|"]
    for step in JOB_STEPS:
        st = t["steps"].get(step)
        if st:
            out.append(f"| {step} | {st['count']} | {_fmt(st['total_s'])} | {st['mean_s']:.1f}s | {st['max_s']:.1f}s |")

    if t["llm"]:
        out += ["", "## LLM calls", "",
                "| Agent (model) | Calls | Prompt tok | Completion tok | Time | Max call | Rate-limit wait |",
                "|---|---:|---:|---:|---:|---:|---:|"]
        for name, r in t["llm"].items():
            out.append(f"| {name} | {r['calls']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | "
                       f"{_fmt(r['seconds'])} | {r['max_s']:.1f}s | {r['rate_limit_wait_s']:.0f}s |")

    out += ["", "## Section jobs (slowest first)", "",
            "| Job | Iter | Wall | Parser | Guardrails | Critic | Start → end |",
            "|---|---:|---:|---:|---:|---:|---|"]
    for name, j in sorted(t["jobs"].items(), key=lambda kv: -kv[1]["wall_s"]):
        out.append(f"| {name} | {j['iterations']} | {_fmt(j['wall_s'])} | {j.get('parser', 0):.1f}s | "
                   f"{j.get('guardrails', 0):.2f}s | {j.get('critic', 0):.1f}s | "
                   f"{j['start_s']:.0f}s → {j['end_s']:.0f}s |")
    return "\n".join(out) + "\n"
