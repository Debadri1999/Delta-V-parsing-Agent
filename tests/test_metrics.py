from __future__ import annotations

from dds_agent.evaluation.metrics import compare_rows, evaluate, norm, token_f1
from dds_agent.schema.dds import CommandSequence, DdsDocument, SequenceRow


def test_norm_maps_booleans_and_class_prefix():
    assert norm("T") == norm("True") == "true"
    assert norm("Class: _V12_120_M") == "_v12_120_m"


def test_token_f1():
    assert token_f1("Set Agitator to STOP", "set agitator to stop") == 1.0
    assert token_f1("", "") == 1.0
    assert 0 < token_f1("Pressure Supply Valve", "Pressure Valve") < 1


def test_compare_rows_counts_missing_extra_and_prose():
    gt = [{"device": "A", "description": "Drain Valve", "ownership": "Private"},
          {"device": "B", "description": "Pump", "ownership": "Shared"}]
    pred = [{"device": "A", "description": "drain valve", "ownership": "Private"},
            {"device": "C", "description": "x", "ownership": "Private"}]
    s = compare_rows("control_module_interface", gt, pred, "device", ["device", "description", "ownership"])
    assert s.matched_rows == 1 and s.missing_rows == ["b"] and s.extra_rows == ["c"]
    assert s.correct_cells == 3 and s.cell_recall == 0.5


def test_evaluate_end_to_end():
    truth = DdsDocument(module="_EM-X", sections={"hold_monitors": {"rows": [{"number": "01", "description": "Lost"}]}},
                        commands=[CommandSequence(command="HOLD", block="COMMAND_00000", value=0, rows=[
                            SequenceRow(step="S0000 / Start", item="A0010", delay="N/A", description="Go")])])
    pred = truth.model_copy(deep=True)
    result = evaluate(pred, truth)
    assert result["overall"]["cell_f1"] == 1.0
