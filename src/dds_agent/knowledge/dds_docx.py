"""Read hand-written DDS Word documents into :class:`DdsDocument` objects.

Used twice:

* as **exemplars** for retrieval (how engineers phrase each section), and
* as **ground truth** for the evaluation harness.

A DDS docx can hold several EM classes; each starts at a Heading 1 that contains
the class name in parentheses, e.g. ``Mixing Equipment Module (_EM-MIXER-CL)``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from dds_agent.schema.dds import CommandSequence, DdsDocument, SequenceRow

logger = logging.getLogger(__name__)

_MODULE_RE = re.compile(r"\((_?\s*EM-[A-Z0-9\-]+)\)")
_COMMAND_RE = re.compile(r"^(.+?)\s*\((COMMAND_(\d+))\)\s*$")


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\xa0", " ")).strip()


def _iter_blocks(document: docx.document.Document) -> Iterator[Paragraph | Table]:
    for el in document.element.body.iterchildren():
        if el.tag == qn("w:p"):
            yield Paragraph(el, document)
        elif el.tag == qn("w:tbl"):
            yield Table(el, document)


def _table_rows(table: Table) -> list[list[str]]:
    """Table as text rows; horizontally merged cells appear once."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells, seen = [], set()
        for cell in row.cells:
            if id(cell._tc) in seen:
                continue
            seen.add(id(cell._tc))
            cells.append(_clean(cell.text))
        rows.append(cells)
    return rows


def _heading_level(p: Paragraph) -> int:
    name = (p.style.name or "") if p.style is not None else ""
    m = re.match(r"Heading (\d)", name)
    return int(m.group(1)) if m else 0


def _parse_value_cell(text: str) -> dict[str, str]:
    """Parse ``type: X / min: a / max: b / default: c`` (one item per line)."""
    out: dict[str, str] = {}
    for line in re.split(r"\n|\s/\s", text):
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _section_for(title: str, module: str) -> str | None:
    t = title.lower()
    table = {
        "overview": "overview", "control module interface": "control_module_interface",
        "parameter interface": "parameter_interface", "alarms": "alarms",
        "historical data collection": "history", "hold monitors": "hold_monitors",
        "sentinel monitors": "sentinel_monitors", "command sequence properties": "command_properties",
        "function block diagram custom logic": "fbd_logic", "linked composites": "linked_composites",
        "embedded composites": "embedded_composites", "operator interfaces": "operator_interfaces",
        "faceplate interface": "operator_interfaces", "detail faceplate interface": "operator_interfaces",
        "dynamo interface": "operator_interfaces",
    }
    if t.endswith("properties") and module.lower().strip("_ ") in t.replace(" ", ""):
        return "properties"
    return table.get(t)


def _rows_for(section: str, rows: list[list[str]]) -> list[dict]:
    body = [r for r in rows[1:] if any(c for c in r)]
    out: list[dict] = []
    for r in body:
        r = r + [""] * 8
        if section == "properties":
            out.append({"property": r[0], "value": r[1]})
        elif section == "control_module_interface":
            out.append({"device": r[0], "module_class": re.sub(r"^Class:\s*", "", r[1]),
                        "description": r[2], "ownership": r[3]})
        elif section == "parameter_interface":
            v = _parse_value_cell(r[3])
            out.append({"name": r[0], "description": r[1], "parameter_number": r[2],
                        "type": v.get("type", ""), "min": v.get("min", ""), "max": v.get("max", ""),
                        "default": v.get("default", "")})
        elif section == "alarms":
            out.append({"alarm": r[0], "alarm_name": r[1], "function": r[2], "priority": r[3],
                        "enabled": r[4], "inverted": r[5], "alarm_type": r[6]})
        elif section == "history":
            out.append({"value_recorded": r[0], "enable": r[1], "display_representation": r[2],
                        "data_characteristic": r[3], "sampling_period": r[4], "deviation": r[5], "at_least": r[6]})
        elif section in ("hold_monitors", "sentinel_monitors"):
            out.append({"number": r[0], "description": r[1]})
        elif section == "fbd_logic":
            out.append({"block": r[0], "type": r[1], "description": r[2]})
        elif section in ("linked_composites", "embedded_composites"):
            out.append({"name": r[0], "definition": r[1], "description": r[2]})
        elif section == "command_properties":
            out.append({"command": r[0], "max_time": r[1]})
    return out


def read_dds_docx(path: Path) -> list[DdsDocument]:
    """Parse every EM class section of a DDS docx.

    Args:
        path: The ``.docx`` file.

    Returns:
        One :class:`DdsDocument` per EM class found (only sections with content are filled).
    """
    document = docx.Document(str(path))
    docs: list[DdsDocument] = []
    current: DdsDocument | None = None
    section: str | None = None
    command: CommandSequence | None = None
    texts: dict[str, list[str]] = {}

    def flush_text() -> None:
        if current is not None:
            for sid, parts in texts.items():
                if parts and sid not in current.sections:
                    current.sections[sid] = {"text": "\n".join(parts)}
        texts.clear()

    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            level, text = _heading_level(block), _clean(block.text)
            if level == 1:
                flush_text()
                m = _MODULE_RE.search(text)
                current = DdsDocument(module=m.group(1).replace(" ", ""), description=text) if m else None
                if current is not None:
                    docs.append(current)
                section, command = None, None
                continue
            if current is None:
                continue
            if level in (2, 3, 4):
                cm = _COMMAND_RE.match(text)
                if cm and level == 3:
                    command = CommandSequence(command=cm.group(1).strip(), block=cm.group(2), value=int(cm.group(3)))
                    current.commands.append(command)
                    section = "command_sequences"
                    continue
                if level == 4 and command is not None:
                    continue  # "<CMD> Sequence Diagram/Table" sub-headings
                section = _section_for(text, current.module) or section
                command = None if section != "command_sequences" else command
                continue
            if text and section in ("overview", "linked_composites", "embedded_composites", "operator_interfaces"):
                texts.setdefault(section, []).append(text)
        elif current is not None and section is not None:
            rows = _table_rows(block)
            if not rows:
                continue
            if section == "command_sequences" and command is not None:
                for r in rows[1:]:
                    r = r + [""] * 4
                    if r[1]:
                        command.rows.append(SequenceRow(step=r[0].replace("\n", " / "), item=r[1],
                                                        delay=r[2].replace("\n", " ").strip(), description=r[3]))
            elif section == "command_properties":
                for row in _rows_for(section, rows):
                    cm = _COMMAND_RE.match(row["command"])
                    if cm:
                        current.sections.setdefault("command_properties", {"rows": []})["rows"].append(row)
            else:
                current.sections.setdefault(section, {"rows": []})["rows"].extend(_rows_for(section, rows))
    flush_text()
    for d in docs:
        props = {r["command"]: r["max_time"] for r in d.rows("command_properties")}
        for c in d.commands:
            c.max_time = props.get(f"{c.command} ({c.block})", c.max_time)
    logger.info("Read %d DDS module section(s) from %s", len(docs), path.name)
    return docs
