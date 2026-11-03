"""Typed model of a Detailed Design Specification (DDS) for one equipment module class.

The section list mirrors the hand-written AstraZeneca DDS (``Spec EM-*.docx``):
Properties, Control Module Interface, Parameter Interface, Alarms, Historical Data
Collection, Hold/Sentinel Monitors, Command Sequences, FBD Custom Logic,
Linked/Embedded Composites and Operator Interfaces.

Every row carries ``source_refs`` (FHX chunk ids) so each statement in the PDF is
traceable to the export.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Row(BaseModel):
    """Base row: tolerant of extra keys, always citable."""

    model_config = ConfigDict(extra="ignore")
    source_refs: list[str] = Field(default_factory=list, description="FHX source ids this row was taken from")


class PropertyRow(Row):
    property: str
    value: str


class DeviceRow(Row):
    device: str = Field(description="Module block NAME, e.g. AGITATOR")
    module_class: str = Field(description="Class of the module block, e.g. _V12_120_M")
    description: str
    ownership: str = Field(description="Private or Shared")


class ParameterRow(Row):
    name: str
    description: str
    parameter_number: str = Field(description="OPnnn / TPnnn / RPnnn slot, or N/A")
    type: str = Field(description="e.g. Floating point, Named Set: EM_X, Boolean, String, External Reference")
    min: str = "N/A"
    max: str = "N/A"
    default: str = ""


class AlarmRow(Row):
    alarm: str = Field(description="Readable alarm name, e.g. Fail Alarm")
    alarm_name: str = Field(description="Alarm parameter NAME, e.g. FAIL_ALM")
    function: str
    priority: str
    enabled: str = Field(description="T or F")
    inverted: str = Field(description="T or F")
    alarm_type: str


class HistoryRow(Row):
    value_recorded: str
    enable: str
    display_representation: str
    data_characteristic: str
    sampling_period: str
    deviation: str
    at_least: str


class MonitorRow(Row):
    number: str = Field(description="Two-digit slot number 01..16")
    description: str


class SequenceRow(Row):
    step: str = Field(description="'<step NAME> / <step DESCRIPTION>', e.g. 'S0010 / Hold State'")
    item: str = Field(description="Action or transition NAME, e.g. A0020, EXIT, T0010, END")
    delay: str = Field(description="Delay condition in words, or N/A")
    description: str


class CommandSequence(Row):
    command: str = Field(description="Command (named-set entry) name, e.g. AGITATE")
    block: str = Field(description="Command composite block NAME, e.g. COMMAND_00002")
    value: int = Field(description="Named-set entry value")
    max_time: str = Field(default="0", description="Command failure maximum time (sec)")
    rows: list[SequenceRow] = Field(default_factory=list)


class LogicRow(Row):
    block: str
    type: str = Field(description="Block definition, e.g. ACT, CND, CALC")
    description: str = Field(description="Plain-English description of the block logic")


class CompositeRow(Row):
    name: str
    definition: str
    description: str


class TextOutput(BaseModel):
    """Output of a narrative section."""

    model_config = ConfigDict(extra="ignore")
    text: str
    source_refs: list[str] = Field(default_factory=list)


class TableOutput(BaseModel):
    """Output of a table section. ``rows`` is validated against the section's row model."""

    model_config = ConfigDict(extra="ignore")
    rows: list[dict] = Field(default_factory=list)
    note: str = Field(default="", description="Sentence shown when the table is empty or needs context")


SectionKind = Literal["text", "table", "commands"]


@dataclass(frozen=True)
class SectionSpec:
    """Static definition of one DDS section.

    Attributes:
        id: Stable identifier used in plans, prompts, guardrails and evaluation.
        title: Heading used in the PDF (``{module}`` is substituted).
        kind: ``text``, ``table`` or ``commands`` (one sub-extraction per command).
        row_model: Row type for table sections.
        key: Row field that identifies a row (for merging, critique and evaluation).
        columns: (field, header) pairs rendered in the PDF, in order.
        grounded: Fields that must be copied verbatim from the FHX (checked by guardrails).
        intro: Sentence printed under the heading.
        fixed_count: Exact number of rows required (e.g. 16 monitor slots).
    """

    id: str
    title: str
    kind: SectionKind
    row_model: type[Row] | None = None
    key: str = ""
    columns: tuple[tuple[str, str], ...] = ()
    grounded: tuple[str, ...] = ()
    intro: str = ""
    fixed_count: int | None = None


SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec("overview", "Overview", "text"),
    SectionSpec("properties", "{module} Properties", "table", PropertyRow, "property",
                (("property", "Property"), ("value", "Value"))),
    SectionSpec("control_module_interface", "Control Module Interface", "table", DeviceRow, "device",
                (("device", "Device No."), ("module_class", "Interface"), ("description", "Description"),
                 ("ownership", "Ownership")),
                grounded=("device", "module_class"),
                intro="The following control module device blocks are used by the {module} equipment module class."),
    SectionSpec("parameter_interface", "Parameter Interface", "table", ParameterRow, "name",
                (("name", "Parameter Name"), ("description", "Description"), ("parameter_number", "Parameter Number"),
                 ("type", "Type"), ("min", "Min"), ("max", "Max"), ("default", "Default")),
                grounded=("name",),
                intro="The table below describes the parameters of the equipment module class, which are used by "
                      "the equipment module while communicating with other entities."),
    SectionSpec("alarms", "Alarms", "table", AlarmRow, "alarm_name",
                (("alarm", "Alarm"), ("alarm_name", "Alarm Name"), ("function", "Function"),
                 ("priority", "Default Priority"), ("enabled", "Enabled"), ("inverted", "Inverted"),
                 ("alarm_type", "Alarm Type")),
                grounded=("alarm_name", "priority", "alarm_type"),
                intro="Alarms generated by the Equipment Module have default priorities as listed in the table below."),
    SectionSpec("history", "Historical Data Collection", "table", HistoryRow, "value_recorded",
                (("value_recorded", "Value Recorded"), ("enable", "Enable"),
                 ("display_representation", "Display Representation"), ("data_characteristic", "Data Characteristic"),
                 ("sampling_period", "Sampling Period"), ("deviation", "Deviation"), ("at_least", "At Least"))),
    SectionSpec("hold_monitors", "Hold Monitors", "table", MonitorRow, "number",
                (("number", "No."), ("description", "Description")), fixed_count=16,
                intro="The following table lists the hold monitor failure conditions for this Equipment Module class."),
    SectionSpec("sentinel_monitors", "Sentinel Monitors", "table", MonitorRow, "number",
                (("number", "No."), ("description", "Description")), fixed_count=16,
                intro="The sentinel monitor conditions monitor the following EM devices for failures and may be "
                      "enabled and disabled as needed during execution of the command sequence logic. Individual "
                      "operator bypasses are provided for each device sentinel monitor. Failure of a device with an "
                      "enabled failure condition that is not bypassed will result in the EM failsafe command sequence "
                      "(HOLD) being executed."),
    SectionSpec("command_sequences", "Command Sequences", "commands", SequenceRow, "item",
                (("step", "Step"), ("item", "Item"), ("delay", "Delay"), ("description", "Description")),
                grounded=("item",)),
    SectionSpec("fbd_logic", "Function Block Diagram Custom Logic", "table", LogicRow, "block",
                (("block", "Block"), ("type", "Type"), ("description", "Description")), grounded=("block", "type")),
    SectionSpec("linked_composites", "Linked Composites", "table", CompositeRow, "name",
                (("name", "Name"), ("definition", "Definition"), ("description", "Description")),
                grounded=("name", "definition")),
    SectionSpec("embedded_composites", "Embedded Composites", "table", CompositeRow, "name",
                (("name", "Name"), ("definition", "Definition"), ("description", "Description")),
                grounded=("name",)),
    SectionSpec("operator_interfaces", "Operator Interfaces", "text"),
)

SECTION_BY_ID: dict[str, SectionSpec] = {s.id: s for s in SECTIONS}


class SectionQA(BaseModel):
    """Quality record for one section, produced by the orchestrator."""

    section_id: str
    score: float = 0.0
    iterations: int = 0
    passed: bool = False
    guardrail_errors: list[str] = Field(default_factory=list)
    guardrail_warnings: list[str] = Field(default_factory=list)
    open_issues: list[str] = Field(default_factory=list)
    critic_summary: str = ""


class DdsDocument(BaseModel):
    """A complete generated DDS for one module class."""

    module: str
    description: str = ""
    source_file: str = ""
    sections: dict[str, dict] = Field(default_factory=dict, description="section id -> TextOutput/TableOutput dump")
    commands: list[CommandSequence] = Field(default_factory=list)
    qa: list[SectionQA] = Field(default_factory=list)
    released: bool = False
    release_reason: str = ""
    models: dict[str, str] = Field(default_factory=dict)
    generated_at: str = ""

    def rows(self, section_id: str) -> list[dict]:
        """Rows of a table section (empty if missing)."""
        return list(self.sections.get(section_id, {}).get("rows", []))
