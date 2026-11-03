"""Render a :class:`DdsDocument` to a single PDF in the house DDS layout.

Layout follows the hand-written AstraZeneca DDS: one chapter per EM class with
numbered sections (Overview, Properties, Control Module Interface, ... Operator
Interfaces), followed by a QA appendix (critic scores, guardrail findings and open
questions for the reviewing engineer). The cover states whether the document passed
the release gate.

Pure-Python (fpdf2) so it runs anywhere without native libraries.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from dds_agent import __version__
from dds_agent.schema.dds import SECTION_BY_ID, DdsDocument

logger = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/ariali.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf", "/Library/Fonts/Arial Italic.ttf"),
]

_TYPOGRAPHY = {"‐": "-", "‑": "-", "‒": "-", " ": " ", " ": " ", " ": " ",
               "​": ""}

GREEN = (0, 110, 60)
RED = (160, 20, 20)
GREY = (110, 110, 110)
HEAD_FILL = (220, 220, 220)


class DdsPdf(FPDF):
    """FPDF with DDS header/footer and Unicode font discovery."""

    def __init__(self, doc: DdsDocument) -> None:
        super().__init__(orientation="P", unit="mm", format="Letter")
        self.doc = doc
        self.set_auto_page_break(True, margin=18)
        self.set_margins(18, 16, 18)
        self.body_font = self._register_fonts()
        self.alias_nb_pages()

    def _register_fonts(self) -> str:
        for regular, bold, italic in _FONT_CANDIDATES:
            if Path(regular).exists() and Path(bold).exists():
                self.add_font("Body", "", regular)
                self.add_font("Body", "B", bold)
                self.add_font("Body", "I", italic if Path(italic).exists() else regular)
                return "Body"
        logger.warning("No Unicode TTF font found; non-Latin-1 characters will be replaced")
        return "Helvetica"

    def safe(self, text: object) -> str:
        """Text the active font can encode."""
        s = "" if text is None else str(text)
        for odd, plain in _TYPOGRAPHY.items():  # glyphs common fonts lack
            s = s.replace(odd, plain)
        if self.body_font == "Body":
            return s
        return s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'").encode(
            "latin-1", "replace").decode("latin-1")

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(self.body_font, "", 8)
        self.set_text_color(*GREY)
        status = "RELEASED FOR ENGINEERING REVIEW" if self.doc.released else "DRAFT - NOT RELEASED"
        self.cell(0, 5, self.safe(f"DDS {self.doc.module}"), new_x=XPos.LMARGIN, new_y=YPos.TOP)
        self.cell(0, 5, status, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-13)
        self.set_font(self.body_font, "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.cell(0, 4, self.safe(f"Generated {self.doc.generated_at[:10]} by dds-agent {__version__} from "
                                  f"{self.doc.source_file}"), align="C")
        self.set_text_color(0, 0, 0)

    # ---------------------------------------------------------------- helpers

    def heading(self, number: str, title: str, level: int) -> None:
        sizes = {1: 15, 2: 12.5, 3: 11, 4: 10}
        if self.get_y() > self.h - 45:
            self.add_page()
        self.ln(3 if level > 1 else 1)
        self.set_font(self.body_font, "B", sizes.get(level, 10))
        text = self.safe(f"{number} {title}".strip())
        self.start_section(text, level=level - 1)
        self.multi_cell(0, 6.5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def para(self, text: str, size: float = 9.5, style: str = "", color: tuple[int, int, int] | None = None) -> None:
        self.set_font(self.body_font, style, size)
        if color:
            self.set_text_color(*color)
        self.multi_cell(0, 4.8, self.safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0, 0, 0)
        self.ln(1.5)

    def grid(self, headers: list[str], rows: list[list[str]], widths: list[float] | None = None,
              size: float = 8) -> None:
        if not rows:
            self.para("(no rows)", color=GREY)
            return
        self.set_font(self.body_font, "", size)
        heading_style = FontFace(emphasis="BOLD", fill_color=HEAD_FILL)
        with super().table(col_widths=widths, headings_style=heading_style, line_height=4.2,
                           text_align="LEFT", first_row_as_headings=True, repeat_headings=1,
                           padding=1) as t:
            t.row([self.safe(h) for h in headers])
            for r in rows:
                t.row([self.safe(c) for c in r])
        self.ln(3)


def _section_table(pdf: DdsPdf, sid: str, doc: DdsDocument, widths: list[float] | None = None) -> None:
    spec = SECTION_BY_ID[sid]
    content = doc.sections.get(sid)
    if spec.intro:
        pdf.para(spec.intro.format(module=doc.module))
    if content is None:
        pdf.para("Section not generated in this run.", color=RED)
        return
    rows = content.get("rows", [])
    note = content.get("note", "")
    if rows:
        cols = list(spec.columns)
        pdf.grid([h for _, h in cols], [[_cell(sid, f, r.get(f, "")) for f, _ in cols] for r in rows], widths)
    if note:
        pdf.para(note, size=9, style="I" if rows else "")
    elif not rows:
        pdf.para("No entries.")


def _cell(sid: str, field: str, value: object) -> str:
    if sid == "control_module_interface" and field == "module_class" and value:
        return f"Class: {value}"
    return "" if value is None else str(value)


def _qa_status(doc: DdsDocument, sid: str) -> str:
    q = [x for x in doc.qa if x.section_id == sid or x.section_id.startswith(sid + ":")]
    if not q:
        return ""
    score = min(x.score for x in q)
    ok = all(x.passed for x in q)
    return f"QA: {'passed' if ok else 'needs review'} - critic score {score:.1f}/10"


def render_pdf(doc: DdsDocument, path: Path) -> Path:
    """Write the DDS PDF.

    Args:
        doc: Generated document.
        path: Output ``.pdf`` path.

    Returns:
        ``path``.
    """
    pdf = DdsPdf(doc)
    pdf.set_title(f"DDS {doc.module}")
    pdf.set_author("dds-agent")
    _cover(pdf, doc)
    pdf.add_page()
    pdf.insert_toc_placeholder(_toc, pages=1, allow_extra_pages=True)
    _chapter(pdf, doc)
    _appendix(pdf, doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def _cover(pdf: DdsPdf, doc: DdsDocument) -> None:
    pdf.add_page()
    pdf.ln(30)
    pdf.set_font(pdf.body_font, "B", 22)
    pdf.multi_cell(0, 10, "Detailed Design Specification", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(pdf.body_font, "", 14)
    pdf.multi_cell(0, 8, pdf.safe(f"{doc.description or 'Equipment Module'} ({doc.module})"), align="C",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(12)
    color = GREEN if doc.released else RED
    pdf.set_draw_color(*color)
    pdf.set_text_color(*color)
    pdf.set_font(pdf.body_font, "B", 13)
    label = "RELEASED FOR ENGINEERING REVIEW" if doc.released else "DRAFT - NOT RELEASED"
    pdf.multi_cell(0, 9, label, border=1, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(pdf.body_font, "", 9.5)
    pdf.multi_cell(0, 5, pdf.safe(f"Release gate: {doc.release_reason}"), align="C",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.set_draw_color(0, 0, 0)
    pdf.ln(10)
    pdf.grid(["Item", "Value"], [
        ["Source export", doc.source_file],
        ["Equipment module class", doc.module],
        ["Generated", doc.generated_at.replace("T", " ")],
        ["Extraction model", doc.models.get("generator", "")],
        ["Review model", doc.models.get("critic", "")],
        ["Generator", f"dds-agent {__version__}"],
    ], widths=[45, 135], size=9)
    pdf.para("Every table in this document was produced by an extraction agent from the FHX export, verified by "
             "automated guardrails (schema, literal grounding against the export, structural coverage) and scored "
             "by an independent review agent. A DeltaV engineer must review and approve this document before it "
             "is used in a controlled (GMP) specification. Open findings are listed in Appendix A.",
             size=8.5, color=GREY)


def _toc(pdf: DdsPdf, outline: list) -> None:
    pdf.set_font(pdf.body_font, "B", 15)
    pdf.cell(0, 10, "Table of Contents", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    for entry in outline:
        if entry.level > 2:
            continue
        pdf.set_font(pdf.body_font, "B" if entry.level == 0 else "", 10 if entry.level == 0 else 9)
        indent = 5 * entry.level
        pdf.set_x(pdf.l_margin + indent)
        width = pdf.epw - indent - 12
        pdf.cell(width, 5.5, pdf.safe(entry.name)[:110], link=pdf.add_link(page=entry.page_number))
        pdf.cell(12, 5.5, str(entry.page_number), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _chapter(pdf: DdsPdf, doc: DdsDocument) -> None:
    n = "1"
    pdf.heading(n, f"{doc.description or 'Equipment Module'} ({doc.module})", 1)

    pdf.heading(f"{n}.1", "Overview", 2)
    ov = doc.sections.get("overview", {})
    pdf.para(ov.get("text") or "Overview not generated.")

    pdf.heading(f"{n}.2", f"{doc.module} Properties", 2)
    _section_table(pdf, "properties", doc, widths=[55, 125])

    pdf.heading(f"{n}.3", "Control Module Interface", 2)
    _section_table(pdf, "control_module_interface", doc, widths=[38, 48, 64, 30])

    pdf.heading(f"{n}.4", "Parameter Interface", 2)
    _section_table(pdf, "parameter_interface", doc, widths=[34, 44, 17, 33, 14, 14, 24])

    pdf.heading(f"{n}.5", "Alarms", 2)
    _section_table(pdf, "alarms", doc, widths=[20, 24, 42, 25, 16, 16, 37])

    pdf.heading(f"{n}.6", "Historical Data Collection", 2)
    _section_table(pdf, "history", doc, widths=[30, 16, 30, 30, 26, 20, 28])

    pdf.heading(f"{n}.7", "Failure Conditions", 2)
    pdf.para("The following tables list the failure conditions for this Equipment Module class.")
    pdf.heading(f"{n}.7.1", "Hold Monitors", 3)
    _section_table(pdf, "hold_monitors", doc, widths=[20, 160])
    pdf.heading(f"{n}.7.2", "Sentinel Monitors", 3)
    _section_table(pdf, "sentinel_monitors", doc, widths=[20, 160])

    pdf.heading(f"{n}.8", "Command Sequences", 2)
    pdf.heading(f"{n}.8.1", "Command Sequence Properties", 3)
    pdf.grid(["Command", "Command Failure Maximum Time (sec)"],
              [[f"{c.command} ({c.block})", c.max_time] for c in doc.commands], widths=[110, 70], size=8.5)
    for i, cmd in enumerate(doc.commands, start=2):
        pdf.heading(f"{n}.8.{i}", f"{cmd.command} ({cmd.block})", 3)
        status = _qa_status(doc, f"command_sequences:{cmd.block}")
        pdf.heading(f"{n}.8.{i}.1", f"{cmd.command} Sequence Table", 4)
        rows, last_step = [], None
        for r in cmd.rows:
            rows.append([r.step if r.step != last_step else "", r.item, r.delay, r.description])
            last_step = r.step
        pdf.grid(["Step", "Item", "Delay", "Description"], rows, widths=[42, 18, 50, 70])
        if status:
            pdf.para(status, size=7.5, color=GREY)

    pdf.heading(f"{n}.9", "Function Block Diagram Custom Logic", 2)
    _section_table(pdf, "fbd_logic", doc, widths=[32, 18, 130])

    pdf.heading(f"{n}.10", "Linked Composites", 2)
    _section_table(pdf, "linked_composites", doc, widths=[40, 45, 95])

    pdf.heading(f"{n}.11", "Embedded Composites", 2)
    _section_table(pdf, "embedded_composites", doc, widths=[40, 45, 95])

    pdf.heading(f"{n}.12", "Operator Interfaces", 2)
    oi = doc.sections.get("operator_interfaces", {})
    pdf.para(oi.get("text") or "Operator interfaces not generated.")


def _appendix(pdf: DdsPdf, doc: DdsDocument) -> None:
    pdf.add_page()
    pdf.heading("Appendix A.", "Quality Assurance Report", 1)
    pdf.para(f"Release decision: {'RELEASED' if doc.released else 'NOT RELEASED'} - {doc.release_reason}",
             style="B", color=GREEN if doc.released else RED)
    pdf.para("Each section was drafted by the extraction agent, checked by deterministic guardrails and scored "
             "0-10 by the review agent (score = lowest of completeness, faithfulness and conformance). Guardrail "
             "errors cap a section at 6. Sections that did not pass were revised up to the iteration limit.",
             size=8.5, color=GREY)
    rows = [[q.section_id, f"{q.score:.1f}", str(q.iterations), "pass" if q.passed else "review",
             str(len(q.guardrail_errors)), str(len(q.open_issues))] for q in doc.qa]
    pdf.grid(["Section", "Score", "Iter.", "Status", "Guardrail errors", "Open issues"], rows,
              widths=[70, 18, 14, 22, 28, 28], size=8)

    pdf.heading("A.1", "Findings for the reviewing engineer", 2)
    pdf.para("These are findings to confirm, not established defects. Guardrail errors are verified by code; "
             "reviewer issues come from the review model.", size=8.5, color=GREY)
    findings = []
    for q in doc.qa:
        for e in q.guardrail_errors:
            findings.append([q.section_id, "guardrail error", e])
        for w in q.guardrail_warnings:
            findings.append([q.section_id, "guardrail warning", w])
        if not q.passed:
            for i in q.open_issues:
                findings.append([q.section_id, "reviewer", i])
    if findings:
        pdf.grid(["Section", "Raised by", "Finding"], findings, widths=[45, 28, 107], size=7.5)
    else:
        pdf.para("No open findings.")
    pdf.para(f"Document rendered {datetime.now().strftime('%d-%b-%Y %H:%M')}.", size=8, color=GREY)
