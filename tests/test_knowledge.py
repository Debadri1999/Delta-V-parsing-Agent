from __future__ import annotations

from pathlib import Path

import docx

from dds_agent.knowledge.dds_docx import read_dds_docx
from dds_agent.knowledge.store import KnowledgeStore, load_rule_passages


def _make_dds(path: Path) -> None:
    d = docx.Document()
    d.add_heading("Mini Test Equipment Module (_EM-MINI-CL)", level=1)
    d.add_heading("Control Module Interface", level=2)
    t = d.add_table(rows=2, cols=4)
    for i, h in enumerate(["Device No.", "Interface", "Description", "Ownership"]):
        t.rows[0].cells[i].text = h
    for i, v in enumerate(["DRN_VLV", "Class: _V_TEST_M", "Drain Valve", "Private"]):
        t.rows[1].cells[i].text = v
    d.add_heading("Command Sequences", level=2)
    d.add_heading("HOLD (COMMAND_00000)", level=3)
    d.add_heading("HOLD Sequence Table", level=4)
    s = d.add_table(rows=2, cols=4)
    for i, v in enumerate(["Step", "Item", "Delay", "Description"]):
        s.rows[0].cells[i].text = v
    for i, v in enumerate(["S0000\nStart", "A0010", "N/A", "Set Message"]):
        s.rows[1].cells[i].text = v
    d.save(str(path))


def test_read_dds_docx(tmp_path):
    p = tmp_path / "dds.docx"
    _make_dds(p)
    (doc,) = read_dds_docx(p)
    assert doc.module == "_EM-MINI-CL"
    assert doc.rows("control_module_interface")[0]["module_class"] == "_V_TEST_M"
    assert doc.commands[0].command == "HOLD" and doc.commands[0].rows[0].step == "S0000 / Start"


def test_rules_are_tagged_by_section():
    sections = {p.section for p in load_rule_passages()}
    assert {"parameter_interface", "command_sequences", "sentinel_monitors", "*"} <= sections


def test_exemplars_exclude_the_module_being_generated(tmp_path):
    _make_dds(tmp_path / "dds.docx")
    kb = KnowledgeStore.load(tmp_path)
    assert "DRN_VLV" in kb.exemplars_for("control_module_interface", "devices", exclude_module="_EM-OTHER-CL")
    assert kb.exemplars_for("control_module_interface", "devices", exclude_module="_EM-MINI-CL") == ""
