from __future__ import annotations

from dds_agent.fhx.source import FhxSource, Selector, SourceBundle
from dds_agent.guardrails.checks import GuardrailContext, check_command, check_document, check_section
from dds_agent.schema.dds import SECTION_BY_ID


def _ctx(src: FhxSource) -> GuardrailContext:
    return GuardrailContext(src, "_EM-MINI-CL", "EM_MINI")


def _bundle(src: FhxSource, **sel) -> SourceBundle:
    return SourceBundle.from_nodes(src.select(Selector(**sel)))


def test_grounded_device_passes(mini_source):
    spec = SECTION_BY_ID["control_module_interface"]
    out = {"rows": [{"device": "DRN_VLV", "module_class": "_V_TEST_M", "description": "Drain Valve",
                     "ownership": "Private"}]}
    rep = check_section(spec, out, _bundle(mini_source, scope="_EM-MINI-CL", keyword="MODULE_BLOCK"), _ctx(mini_source))
    assert rep.ok, rep.render()


def test_invented_value_is_an_error_and_missing_block_is_reported(mini_source):
    spec = SECTION_BY_ID["control_module_interface"]
    out = {"rows": [{"device": "PUMP_9", "module_class": "_V_TEST_M", "description": "x", "ownership": "Private"}]}
    rep = check_section(spec, out, _bundle(mini_source, scope="_EM-MINI-CL", keyword="MODULE_BLOCK"), _ctx(mini_source))
    joined = " ".join(rep.errors)
    assert "ungrounded" in joined and "DRN_VLV" in joined


def test_monitors_need_sixteen_numbered_rows(mini_source):
    spec = SECTION_BY_ID["hold_monitors"]
    rows = [{"number": f"{i:02d}", "description": "Not Used"} for i in range(1, 16)]
    rep = check_section(spec, {"rows": rows}, SourceBundle(), _ctx(mini_source))
    assert any("missing 16" in e for e in rep.errors)


def test_alarm_and_history_coverage(mini_source):
    rep = check_section(SECTION_BY_ID["alarms"], {"rows": [], "note": "none"}, SourceBundle(), _ctx(mini_source))
    assert any("FAIL_ALM" in e for e in rep.errors)
    rep = check_section(SECTION_BY_ID["history"], {"rows": [], "note": "none"}, SourceBundle(), _ctx(mini_source))
    assert any("A_COMMAND.CV" in e for e in rep.errors)


def test_command_coverage_and_naming(mini_source):
    spec = SECTION_BY_ID["command_sequences"]
    bundle = _bundle(mini_source, scope="_EM-MINI-CL", keyword="FUNCTION_BLOCK", name_regex="COMMAND_00000",
                     follow_references=True)
    good = {"command": "HOLD", "block": "COMMAND_00000", "value": 0, "rows": [
        {"step": "S0000 / Start", "item": "A0010", "delay": "N/A", "description": "Set Message"},
        {"step": "S0000 / Start", "item": "EXIT", "delay": "Other Step Actions Complete", "description": "Clear Messages"},
        {"step": "S0000 / Start", "item": "END", "delay": "N/A", "description": "CommandComplete"}]}
    assert check_command(spec, good, bundle, _ctx(mini_source), "COMMAND_00000", "__AAAA0000_00000001__").ok
    bad = dict(good, command="RUN", rows=good["rows"][:1])
    rep = check_command(spec, bad, bundle, _ctx(mini_source), "COMMAND_00000", "__AAAA0000_00000001__")
    joined = " ".join(rep.errors)
    assert "'HOLD'" in joined and "S0000/EXIT" in joined and "END" in joined


def test_cross_section_duplicate_parameter_numbers(mini_source):
    sections = {"parameter_interface": {"rows": [{"name": "A", "parameter_number": "OP001"},
                                                 {"name": "B", "parameter_number": "OP001"}]}}
    assert check_document(sections, _ctx(mini_source)).errors


def test_template_text_and_slot_consistency(mini_source):
    spec = SECTION_BY_ID["parameter_interface"]
    rows = [{"name": "EM_DRN_TM", "description": "Drain Time (sec)", "parameter_number": "N/A",
             "type": "Named Set: <SET>", "min": "N/A", "max": "N/A", "default": "30"}]
    joined = " ".join(check_section(spec, {"rows": rows}, SourceBundle(), _ctx(mini_source)).errors)
    assert "template text" in joined and "OP001_VALUE references EM_DRN_TM" in joined
    rows[0].update(type="Floating point", parameter_number="OP001")
    assert check_section(spec, {"rows": rows}, SourceBundle(), _ctx(mini_source)).ok


def test_sentinel_slots_follow_command_enablement(mini_source):
    spec = SECTION_BY_ID["sentinel_monitors"]
    rows = [{"number": f"{i:02d}", "description": "Not Used"} for i in range(1, 17)]
    rep = check_section(spec, {"rows": rows}, SourceBundle(), _ctx(mini_source))
    assert any("slot 01 is enabled" in e for e in rep.errors)
    rows[0]["description"] = "DRN_VLV Tag Name"
    assert check_section(spec, {"rows": rows}, SourceBundle(), _ctx(mini_source)).ok


def test_empty_fbd_table_with_expression_blocks_is_an_error(mini_source):
    rep = check_section(SECTION_BY_ID["fbd_logic"], {"rows": [], "note": "none"}, SourceBundle(), _ctx(mini_source))
    assert any("SIP" in e for e in rep.errors)
    excluded = {"rows": [], "note": "SIP excluded: standard framework logic."}
    assert check_section(SECTION_BY_ID["fbd_logic"], excluded, SourceBundle(), _ctx(mini_source)).ok


def test_fbd_exclusion_cannot_claim_existing_block_is_absent(mini_source):
    note = {"rows": [], "note": "SIP excluded: not found in source."}
    rep = check_section(SECTION_BY_ID["fbd_logic"], note, SourceBundle(), _ctx(mini_source))
    assert any("claims blocks are absent" in e for e in rep.errors)


def test_missing_parameters_produce_fetch_selectors_and_wrong_defaults_fail(mini_source):
    from dds_agent.orchestrator import _augment

    spec = SECTION_BY_ID["parameter_interface"]
    rep = check_section(spec, {"rows": [], "note": "none"}, SourceBundle(), _ctx(mini_source))
    assert any("EM_DRN_TM" in e for e in rep.errors) and rep.fetch
    bundle = _augment(SourceBundle(), rep, mini_source, "test")
    assert "_EM-MINI-CL::ATTRIBUTE_INSTANCE:EM_DRN_TM" in bundle.refs
    rows = [{"name": "EM_DRN_TM", "description": "Drain Time (sec)", "parameter_number": "OP001",
             "type": "Floating point", "min": "N/A", "max": "N/A", "default": "N/A"}]
    rep = check_section(spec, {"rows": rows}, bundle, _ctx(mini_source))
    assert any("instance value is '30'" in e for e in rep.errors)
