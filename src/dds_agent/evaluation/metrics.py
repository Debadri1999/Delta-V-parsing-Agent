"""Field-level comparison of a generated DDS against a hand-written ground truth.

Rows are matched by the section key (case/space-insensitive). Every non-key cell of
a matched row is then scored:

* **exact** columns (names, classes, numbers, flags) must match after normalization;
* **prose** columns (descriptions, alarm function, delay wording) match when their
  token-overlap F1 is at least :data:`PROSE_THRESHOLD`.

Reported per section: row precision/recall and cell precision/recall/F1, where a
missing row costs all its ground-truth cells and an extra row costs all its cells.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from dds_agent.schema.dds import SECTION_BY_ID, DdsDocument

PROSE_THRESHOLD = 0.6
PROSE_FIELDS: dict[str, set[str]] = {
    "control_module_interface": {"description"},
    "parameter_interface": {"description"},
    "alarms": {"alarm", "function"},
    "fbd_logic": {"description"},
    "linked_composites": {"description"},
    "embedded_composites": {"description"},
    "command_sequences": {"step", "delay", "description"},
}
TABLE_SECTIONS = ("properties", "control_module_interface", "parameter_interface", "alarms", "history",
                  "hold_monitors", "sentinel_monitors", "fbd_logic")
_BOOL = {"t": "true", "true": "true", "yes": "true", "f": "false", "false": "false", "no": "false"}


def norm(value: object) -> str:
    """Normalize a cell for exact comparison."""
    s = re.sub(r"\s+", " ", str(value or "").replace("’", "'").replace("“", '"').replace("”", '"'))
    s = s.strip().strip(".").lower()
    s = re.sub(r"^class:\s*", "", s)
    return _BOOL.get(s, s)


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def token_f1(a: str, b: str) -> float:
    """Token-overlap F1 between two strings (1.0 when both are empty)."""
    ta, tb = tokens(a), tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    common = 0
    pool = list(tb)
    for t in ta:
        if t in pool:
            pool.remove(t)
            common += 1
    if not common:
        return 0.0
    p, r = common / len(ta), common / len(tb)
    return 2 * p * r / (p + r)


def _key(section: str, value: object) -> str:
    k = norm(value)
    if section in ("hold_monitors", "sentinel_monitors"):
        k = k.zfill(2)
    return k


@dataclass
class SectionScore:
    """Comparison result for one section (or one command)."""

    section: str
    gt_rows: int = 0
    pred_rows: int = 0
    matched_rows: int = 0
    gt_cells: int = 0
    pred_cells: int = 0
    correct_cells: int = 0
    prose_similarity: list[float] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    missing_rows: list[str] = field(default_factory=list)
    extra_rows: list[str] = field(default_factory=list)

    @property
    def row_precision(self) -> float:
        return self.matched_rows / self.pred_rows if self.pred_rows else float(self.gt_rows == 0)

    @property
    def row_recall(self) -> float:
        return self.matched_rows / self.gt_rows if self.gt_rows else 1.0

    @property
    def cell_precision(self) -> float:
        return self.correct_cells / self.pred_cells if self.pred_cells else float(self.gt_cells == 0)

    @property
    def cell_recall(self) -> float:
        return self.correct_cells / self.gt_cells if self.gt_cells else 1.0

    @property
    def cell_f1(self) -> float:
        p, r = self.cell_precision, self.cell_recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict:
        sim = self.prose_similarity
        return {
            "section": self.section, "gt_rows": self.gt_rows, "pred_rows": self.pred_rows,
            "matched_rows": self.matched_rows, "row_precision": round(self.row_precision, 3),
            "row_recall": round(self.row_recall, 3), "cell_precision": round(self.cell_precision, 3),
            "cell_recall": round(self.cell_recall, 3), "cell_f1": round(self.cell_f1, 3),
            "mean_prose_similarity": round(sum(sim) / len(sim), 3) if sim else None,
            "missing_rows": self.missing_rows, "extra_rows": self.extra_rows, "mismatches": self.mismatches[:60],
        }


def compare_rows(section: str, gt: list[dict], pred: list[dict], key: str, columns: list[str],
                 key_fn=None) -> SectionScore:
    """Compare two row lists.

    Args:
        section: Section id (selects prose columns).
        gt: Ground-truth rows.
        pred: Predicted rows.
        key: Field used to match rows when ``key_fn`` is not given.
        columns: Fields to score (including the key).
        key_fn: Optional function row -> match key.
    """
    kf = key_fn or (lambda r: _key(section, r.get(key, "")))
    prose = PROSE_FIELDS.get(section, set())
    score = SectionScore(section, gt_rows=len(gt), pred_rows=len(pred))
    score.gt_cells = len(gt) * len(columns)
    score.pred_cells = len(pred) * len(columns)
    pred_by_key: dict[str, dict] = {}
    for r in pred:
        pred_by_key.setdefault(kf(r), r)
    gt_keys = set()
    for g in gt:
        k = kf(g)
        gt_keys.add(k)
        p = pred_by_key.get(k)
        if p is None:
            score.missing_rows.append(k)
            continue
        score.matched_rows += 1
        for c in columns:
            gv, pv = g.get(c, ""), p.get(c, "")
            if c in prose:
                sim = token_f1(str(gv), str(pv))
                score.prose_similarity.append(sim)
                ok = sim >= PROSE_THRESHOLD
            else:
                ok = norm(gv) == norm(pv)
            if ok:
                score.correct_cells += 1
            else:
                score.mismatches.append(f"{k}.{c}: expected {str(gv)!r} got {str(pv)!r}")
    score.extra_rows = sorted(k for k in pred_by_key if k not in gt_keys)
    return score


def evaluate(pred: DdsDocument, truth: DdsDocument) -> dict:
    """Score every section present in the ground truth.

    Returns:
        ``{"sections": [...], "commands": [...], "overall": {...}}``.
    """
    results: list[SectionScore] = []
    for sid in TABLE_SECTIONS:
        if sid not in truth.sections:
            continue
        spec = SECTION_BY_ID[sid]
        cols = [c for c, _ in spec.columns]
        results.append(compare_rows(sid, truth.rows(sid), pred.rows(sid), spec.key, cols))

    cmd_results: list[SectionScore] = []
    pred_cmds = {c.block: c for c in pred.commands}
    for tc in truth.commands:
        pc = pred_cmds.get(tc.block)
        gt_rows = [r.model_dump() for r in tc.rows]
        pr_rows = [r.model_dump() for r in pc.rows] if pc else []
        key_fn = lambda r: f"{norm(str(r.get('step', '')).split('/')[0])}|{norm(r.get('item', ''))}"  # noqa: E731
        s = compare_rows("command_sequences", gt_rows, pr_rows, "item", ["step", "item", "delay", "description"], key_fn)
        s.section = f"command_sequences:{tc.block}"
        if pc is None:
            s.mismatches.insert(0, f"command {tc.block} not generated")
        elif norm(pc.command) != norm(tc.command):
            s.mismatches.insert(0, f"command name expected {tc.command!r} got {pc.command!r}")
        cmd_results.append(s)

    all_scores = results + cmd_results
    tot = SectionScore("overall")
    for s in all_scores:
        tot.gt_rows += s.gt_rows
        tot.pred_rows += s.pred_rows
        tot.matched_rows += s.matched_rows
        tot.gt_cells += s.gt_cells
        tot.pred_cells += s.pred_cells
        tot.correct_cells += s.correct_cells
        tot.prose_similarity += s.prose_similarity
    macro = [s.cell_f1 for s in all_scores]
    overall = tot.to_dict()
    overall.pop("missing_rows")
    overall.pop("extra_rows")
    overall.pop("mismatches")
    overall["macro_cell_f1"] = round(sum(macro) / len(macro), 3) if macro else 0.0
    return {"module": truth.module, "sections": [s.to_dict() for s in results],
            "commands": [s.to_dict() for s in cmd_results], "overall": overall}


def report_markdown(result: dict) -> str:
    """Human-readable evaluation table."""
    lines = [f"# Evaluation: {result['module']}", "",
             "| Section | GT rows | Pred rows | Row P | Row R | Cell P | Cell R | Cell F1 | Prose sim |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s in result["sections"] + result["commands"]:
        sim = "" if s["mean_prose_similarity"] is None else f"{s['mean_prose_similarity']:.2f}"
        lines.append(f"| {s['section']} | {s['gt_rows']} | {s['pred_rows']} | {s['row_precision']:.2f} | "
                     f"{s['row_recall']:.2f} | {s['cell_precision']:.2f} | {s['cell_recall']:.2f} | "
                     f"{s['cell_f1']:.2f} | {sim} |")
    o = result["overall"]
    lines += ["", f"**Overall (micro):** cell P {o['cell_precision']:.3f} / R {o['cell_recall']:.3f} / "
                  f"F1 {o['cell_f1']:.3f}; row recall {o['row_recall']:.3f}; macro cell F1 {o['macro_cell_f1']:.3f}", ""]
    lines.append("## Differences")
    for s in result["sections"] + result["commands"]:
        if s["missing_rows"] or s["extra_rows"] or s["mismatches"]:
            lines.append(f"### {s['section']}")
            if s["missing_rows"]:
                lines.append(f"- missing rows: {', '.join(s['missing_rows'])}")
            if s["extra_rows"]:
                lines.append(f"- extra rows: {', '.join(s['extra_rows'])}")
            lines += [f"- {m}" for m in s["mismatches"][:25]]
    return "\n".join(lines) + "\n"
