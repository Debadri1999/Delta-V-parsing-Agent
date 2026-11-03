"""Knowledge base for retrieval-augmented generation.

Two kinds of passages:

* **Rules** - curated markdown in ``knowledge/kb`` (FHX primer, how each DDS section is
  derived, PCSD conventions). Every passage is tagged with the section it applies to.
* **Exemplars** - sections of hand-written DDS documents (``data/exemplars/*.docx``),
  showing how engineers phrase each section for *other* EM classes.

When generating (or evaluating) module X, exemplars of X itself are excluded so the
agents can never copy the answer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from dds_agent.config import PACKAGE_DIR
from dds_agent.knowledge.dds_docx import read_dds_docx
from dds_agent.rag.retriever import TfidfIndex
from dds_agent.schema.dds import SECTION_BY_ID, DdsDocument

logger = logging.getLogger(__name__)

KB_DIR = PACKAGE_DIR / "knowledge" / "kb"
_HEADING_RE = re.compile(r"^## \[section:([a-z_*]+)\]\s*(.*)$", re.M)
EXEMPLAR_MAX_CHARS = 3000


@dataclass(frozen=True)
class Passage:
    """A retrievable piece of knowledge.

    Attributes:
        kind: ``rule`` or ``exemplar``.
        section: DDS section id it applies to (``*`` = all sections).
        title: Short label.
        text: Passage body.
        module: For exemplars, the EM class the passage describes.
    """

    kind: str
    section: str
    title: str
    text: str
    module: str = ""


def load_rule_passages(kb_dir: Path = KB_DIR) -> list[Passage]:
    """Split the markdown knowledge files into section-tagged passages."""
    passages: list[Passage] = []
    for md in sorted(kb_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        heads = list(_HEADING_RE.finditer(text))
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            passages.append(Passage("rule", h.group(1), h.group(2).strip(), text[h.end():end].strip()))
    return passages


def _table_md(rows: list[dict], columns: list[str], limit: int = 30) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(str(r.get(c, "")).replace("\n", " ") for c in columns) + " |")
    if len(rows) > limit:
        lines.append(f"| ... {len(rows) - limit} more rows |")
    return "\n".join(lines)


def exemplar_passages(doc: DdsDocument) -> list[Passage]:
    """Render each section of a hand-written DDS as an exemplar passage."""
    out: list[Passage] = []
    for sid, content in doc.sections.items():
        spec = SECTION_BY_ID.get(sid)
        if "rows" in content and content["rows"]:
            cols = [c for c, _ in spec.columns] if spec else list(content["rows"][0].keys())
            body = _table_md(content["rows"], cols)
        else:
            body = content.get("text", "")
        if body:
            out.append(Passage("exemplar", sid, f"{doc.module} {sid}", body[:EXEMPLAR_MAX_CHARS], doc.module))
    for cmd in doc.commands:
        rows = [r.model_dump() for r in cmd.rows]
        body = f"Command {cmd.command} ({cmd.block})\n" + _table_md(rows, ["step", "item", "delay", "description"], 40)
        out.append(Passage("exemplar", "command_sequences", f"{doc.module} {cmd.command}",
                           body[:EXEMPLAR_MAX_CHARS], doc.module))
    return out


class KnowledgeStore:
    """Rules plus exemplars, retrievable per DDS section."""

    def __init__(self, rules: list[Passage], exemplars: list[Passage]) -> None:
        self.rules = rules
        self.exemplars = exemplars
        self._ex_index: TfidfIndex[Passage] = TfidfIndex(exemplars, lambda p: f"{p.section} {p.title} {p.text}")
        logger.info("Knowledge store: %d rule passages, %d exemplar passages (%d modules)",
                    len(rules), len(exemplars), len({p.module for p in exemplars}))

    @classmethod
    def load(cls, exemplar_dir: Path | None) -> "KnowledgeStore":
        """Load the bundled rules and every ``*.docx`` exemplar in ``exemplar_dir``."""
        exemplars: list[Passage] = []
        if exemplar_dir and exemplar_dir.is_dir():
            for path in sorted(exemplar_dir.glob("*.docx")):
                if path.name.startswith("~$"):
                    continue
                for doc in read_dds_docx(path):
                    exemplars.extend(exemplar_passages(doc))
        return cls(load_rule_passages(), exemplars)

    def rules_for(self, section_id: str, include_general: bool = True) -> str:
        """All rule passages tagged with ``section_id`` (plus ``*`` passages)."""
        chosen = [p for p in self.rules if p.section == section_id or (include_general and p.section == "*")]
        return "\n\n".join(f"### {p.title}\n{p.text}" for p in chosen)

    def exemplars_for(self, section_id: str, query: str, exclude_module: str, top_k: int = 2) -> str:
        """Best-matching exemplar passages for a section, never from ``exclude_module``."""
        norm = exclude_module.strip("_ ").upper()
        hits = self._ex_index.search(
            f"{section_id} {query}", top_k,
            where=lambda p: p.section == section_id and p.module.strip("_ ").upper() != norm)
        return "\n\n".join(f"### Exemplar from {h.item.module}\n{h.item.text}" for h in hits)
