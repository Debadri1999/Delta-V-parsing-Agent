"""Command-line interface.

Examples::

    dds-agent run input/_EM-EXAMPLE-CL.fhx
    dds-agent run input/_EM-EXAMPLE-CL.fhx --truth "data/exemplars/Spec EM-EXAMPLE-CL.docx"
    dds-agent eval output/EM-EXAMPLE-CL/EM-EXAMPLE-CL_dds.json --truth spec.docx
    dds-agent render output/EM-EXAMPLE-CL/EM-EXAMPLE-CL_dds.json
    dds-agent outline input/_EM-EXAMPLE-CL.fhx
    dds-agent models
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from dds_agent.config import Settings

logger = logging.getLogger("dds_agent")


def _run(args: argparse.Namespace, settings: Settings) -> int:
    from dds_agent.evaluation.harness import run_evaluation
    from dds_agent.orchestrator import Orchestrator

    inputs = sorted(args.input.glob("*.fhx")) if args.input.is_dir() else [args.input]
    if not inputs:
        logger.error("No .fhx files found at %s", args.input)
        return 2
    orch = Orchestrator(settings, workers=args.workers)
    exit_code = 0
    for path in inputs:
        result = orch.run(path, out_dir=args.out, sections=args.sections)
        print(f"\n{'RELEASED' if result.document.released else 'DRAFT'}: {result.pdf_path}")
        print(f"  reason: {result.document.release_reason}")
        print(f"  json:   {result.json_path}\n  qa:     {result.qa_path}")
        print(f"  timing: {result.timing_path} (total {orch.tracer.summary()['total_s']:.1f}s)")
        if args.truth:
            ev = run_evaluation(result.document, args.truth, result.pdf_path.parent)
            o = ev["overall"]
            print(f"  eval:   cell F1 {o['cell_f1']:.3f} (P {o['cell_precision']:.3f} / R {o['cell_recall']:.3f}), "
                  f"macro F1 {o['macro_cell_f1']:.3f}")
        if not result.document.released:
            exit_code = 1
    return exit_code


def _eval(args: argparse.Namespace, settings: Settings) -> int:
    from dds_agent.evaluation.harness import run_evaluation
    from dds_agent.evaluation.metrics import report_markdown
    from dds_agent.schema.dds import DdsDocument

    doc = DdsDocument.model_validate_json(args.dds_json.read_text(encoding="utf-8"))
    result = run_evaluation(doc, args.truth, args.dds_json.parent)
    print(report_markdown(result))
    return 0


def _render(args: argparse.Namespace, settings: Settings) -> int:
    from dds_agent.render.pdf import render_pdf
    from dds_agent.schema.dds import DdsDocument

    doc = DdsDocument.model_validate_json(args.dds_json.read_text(encoding="utf-8"))
    out = args.out or args.dds_json.with_name(args.dds_json.stem.replace("_dds", "") + "_DDS.pdf")
    print(render_pdf(doc, out))
    return 0


def _outline(args: argparse.Namespace, settings: Settings) -> int:
    from dds_agent.fhx.source import FhxSource

    src = FhxSource.load(args.input)
    roots = src.root_blocks("MODULE_CLASS") or src.root_blocks()
    print(src.outline(focus=args.focus or (roots[0] if roots else None)))
    return 0


def _models(args: argparse.Namespace, settings: Settings) -> int:
    from openai import OpenAI

    from dds_agent.config import assert_on_prem

    assert_on_prem(settings.base_url)
    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=60)
    ids = sorted(m.id for m in client.models.list().data)
    for role, model in (("generator", settings.generator_model), ("critic", settings.critic_model)):
        print(f"{role:<10} {model:<28} {'available' if model in ids else 'NOT AVAILABLE'}")
    print("\n" + "\n".join(ids))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dds-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="generate the DDS PDF for an FHX file (or every .fhx in a folder)")
    r.add_argument("input", type=Path)
    r.add_argument("--out", type=Path, help="output folder (default output/<module>)")
    r.add_argument("--truth", type=Path, help="hand-written DDS .docx to evaluate against")
    r.add_argument("--sections", nargs="+", help="only these section ids (debugging)")
    r.add_argument("--max-iterations", type=int, help="parse/critique rounds per section")
    r.add_argument("--generator-model", help="override the extraction model")
    r.add_argument("--critic-model", help="override the review model")
    r.add_argument("--workers", type=int, default=3, help="sections processed in parallel")
    r.set_defaults(func=_run)

    e = sub.add_parser("eval", help="score a generated *_dds.json against a hand-written DDS .docx")
    e.add_argument("dds_json", type=Path)
    e.add_argument("--truth", type=Path, required=True)
    e.set_defaults(func=_eval)

    rd = sub.add_parser("render", help="re-render the PDF from a *_dds.json")
    rd.add_argument("dds_json", type=Path)
    rd.add_argument("--out", type=Path)
    rd.set_defaults(func=_render)

    o = sub.add_parser("outline", help="print the structural outline the cartographer sees")
    o.add_argument("input", type=Path)
    o.add_argument("--focus", help="top-level block to expand")
    o.set_defaults(func=_outline)

    m = sub.add_parser("models", help="check the configured models on GenAI Studio")
    m.set_defaults(func=_models)
    return p


def main(argv: list[str] | None = None) -> None:
    from dds_agent.logging_setup import setup_logging

    args = build_parser().parse_args(argv)
    settings = Settings()
    overrides = {k: v for k, v in {
        "max_iterations": getattr(args, "max_iterations", None),
        "generator_model": getattr(args, "generator_model", None),
        "critic_model": getattr(args, "critic_model", None)}.items() if v}
    if overrides:
        settings = replace(settings, **overrides)
    log_file = settings.output_dir / "dds_agent.log" if args.command == "run" else None
    setup_logging(log_file, args.verbose)
    sys.exit(args.func(args, settings))


if __name__ == "__main__":
    main()
