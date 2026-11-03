"""Agent-loop tests with a scripted LLM (no network)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

from dds_agent.config import Settings
from dds_agent.knowledge.store import KnowledgeStore
from dds_agent.llm.client import UsageLedger
from dds_agent.orchestrator import Orchestrator

FIXTURE = Path(__file__).parent / "fixtures" / "mini_em.fhx.txt"

PLAN = {
    "main_module": "_EM-MINI-CL",
    "command_set": "EM_MINI",
    "commands": [{"block": "COMMAND_00000", "definition": "__AAAA0000_00000001__"}],
    "sections": {
        "control_module_interface": {"selectors": [{"scope": "_EM-MINI-CL", "keyword": "MODULE_BLOCK"}]},
        "hold_monitors": {"selectors": [{"scope": "_EM-MINI-CL", "keyword": "ATTRIBUTE_INSTANCE",
                                         "name_regex": "^HM\\d\\d/DESC$", "deep": True}]},
    },
}

GOOD_DEVICE = {"device": "DRN_VLV", "module_class": "_V_TEST_M", "description": "Drain Valve", "ownership": "Private",
               "source_refs": ["_EM-MINI-CL::MODULE_BLOCK:DRN_VLV"]}
HOLD_ROWS = [{"number": f"{i:02d}", "description": "Module Ownership Lost" if i == 1 else "Not Used"} for i in range(1, 17)]
COMMAND = {"command": "HOLD", "block": "COMMAND_00000", "value": 0, "max_time": "0", "rows": [
    {"step": "S0000 / Start", "item": "A0010", "delay": "N/A", "description": "Set Message"},
    {"step": "S0000 / Start", "item": "EXIT", "delay": "Other Step Actions Complete", "description": "Clear Messages"},
    {"step": "S0000 / Start", "item": "END", "delay": "N/A", "description": "Command Complete"}]}


class ScriptedLLM:
    """Returns canned JSON per agent; the device table is wrong on the first try."""

    def __init__(self) -> None:
        self.ledger = UsageLedger()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def chat_json(self, *, agent: str, model: str, system: str, user: str, **_: object):
        with self._lock:
            self.calls.append(agent)
            device_attempts = sum(a == "parser:control_module_interface" for a in self.calls)
        if agent == "cartographer":
            return PLAN
        if agent.startswith("critic:"):
            return {"completeness": 10, "faithfulness": 10, "conformance": 10, "score": 10, "issues": [], "summary": "ok"}
        section = agent.split(":", 1)[1].split(":")[0]
        if agent == "parser:control_module_interface":
            if device_attempts == 1:  # invented device -> guardrail error -> revision
                return {"rows": [dict(GOOD_DEVICE, device="PUMP_X")]}
            assert "Revision request" in user and "PUMP_X" in user
            return {"rows": [GOOD_DEVICE]}
        if agent.startswith("parser:command_sequences"):
            return COMMAND
        if section == "hold_monitors":
            return {"rows": HOLD_ROWS}
        if section in ("overview", "operator_interfaces"):
            return {"text": "Will drain the vessel.", "source_refs": []}
        return {"rows": [], "note": "Not applicable."}


def _orchestrator(tmp_path: Path) -> tuple[Orchestrator, ScriptedLLM]:
    settings = replace(Settings(), output_dir=tmp_path, max_iterations=3)
    llm = ScriptedLLM()
    return Orchestrator(settings, llm=llm, kb=KnowledgeStore.load(None), workers=2), llm


def test_loop_revises_after_guardrail_failure(tmp_path):
    orch, llm = _orchestrator(tmp_path)
    result = orch.run(FIXTURE, out_dir=tmp_path,
                      sections=["control_module_interface", "hold_monitors", "command_sequences"])
    qa = {q.section_id: q for q in result.document.qa}
    assert qa["control_module_interface"].iterations == 2 and qa["control_module_interface"].passed
    assert result.document.rows("control_module_interface")[0]["device"] == "DRN_VLV"
    assert qa["command_sequences:COMMAND_00000"].passed
    assert result.document.released, result.document.release_reason
    assert result.pdf_path.exists() and result.pdf_path.stat().st_size > 5000
    saved = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert saved["module"] == "_EM-MINI-CL"


def test_gate_blocks_release_when_guardrails_fail(tmp_path):
    orch, _ = _orchestrator(tmp_path)
    result = orch.run(FIXTURE, out_dir=tmp_path, sections=["alarms"])  # scripted reply omits FAIL_ALM
    assert not result.document.released
    assert "guardrail errors in alarms" in result.document.release_reason
