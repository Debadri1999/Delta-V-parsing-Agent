"""Evaluation harness: score a generated DDS JSON against a hand-written DDS docx."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from dds_agent.evaluation.metrics import evaluate, report_markdown
from dds_agent.knowledge.dds_docx import read_dds_docx
from dds_agent.schema.dds import DdsDocument

logger = logging.getLogger(__name__)


def load_truth(docx_path: Path, module: str) -> DdsDocument:
    """Ground truth for ``module`` from a DDS docx.

    Raises:
        LookupError: If the docx has no section for the module.
    """
    want = module.strip("_ ").upper()
    for doc in read_dds_docx(docx_path):
        if doc.module.strip("_ ").upper() == want:
            return doc
    raise LookupError(f"{docx_path.name} has no DDS section for {module}")


def run_evaluation(pred: DdsDocument, truth_docx: Path, out_dir: Path) -> dict:
    """Compare, write ``<module>_eval.json`` / ``.md`` into ``out_dir`` and return the result."""
    truth = load_truth(truth_docx, pred.module)
    result = evaluate(pred, truth)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pred.module.strip("_")
    (out_dir / f"{stem}_eval.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out_dir / f"{stem}_eval.md").write_text(report_markdown(result), encoding="utf-8")
    o = result["overall"]
    logger.info("Evaluation %s: cell P=%.3f R=%.3f F1=%.3f (macro F1 %.3f)", pred.module, o["cell_precision"],
                o["cell_recall"], o["cell_f1"], o["macro_cell_f1"])
    return result
