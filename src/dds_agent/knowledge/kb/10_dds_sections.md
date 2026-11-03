# DDS section rules

How each section of the Detailed Design Specification (DDS) is derived from the FHX export
of one equipment module (EM) class. Copy names, classes, priorities and values exactly as
they appear in the source. Never invent a value: if the source does not contain it, write
`N/A` and say so in the row description or note.

## [section:overview] Overview
Sources: the EM class header (DESCRIPTION), its MODULE_BLOCKs and its command named set.
One or two sentences stating what the EM does, in the style "Will provide setpoint
configurable control of the agitator speed." Base it on the class DESCRIPTION, its devices
and its commands. Refer to commands by their named-set names (HOLD, AGITATE ...), never by block
ids (COMMAND_00002), and to devices by their role. No speculation beyond what the source shows.

## [section:properties] Properties table
Sources: the EM class header and its COMMAND_DRIVEN_ALGORITHM child.
Exactly these rows, in this order:
- Named Set = the COMMAND_SET of the COMMAND_DRIVEN_ALGORITHM (the EM's command named set).
- Description = class DESCRIPTION.
- Scan Rate (sec) = PERIOD.
- Primary Control = PRIMARY_CONTROL_DISPLAY, or `N/A` when empty.
- Detail Display = DETAIL_DISPLAY.
- Faceplate Display = INSTRUMENT_AREA_DISPLAY.

## [section:control_module_interface] Control Module Interface
Sources: the MODULE_BLOCK children of the EM class.
One row per `MODULE_BLOCK` directly inside the EM class (not inside other classes).
- device = MODULE_BLOCK NAME.
- module_class = the MODULE attribute (the control module class), e.g. `_V12_120_M`.
- description = the functional role of the device. Use the MODULE_BLOCK DESCRIPTION when it
  names the device's function (correct obvious typos such as "Pressue" -> "Pressure").
  When the DESCRIPTION is only a generic class text (e.g. "Modified PCSD Analogue Input
  Module V12.0.0"), derive the role from the device NAME instead (LEVEL_XMTR -> "Level
  Transmitter", FLOW_METER2 -> "Flow Meter 2", SPEED on a mixer EM -> "Mixer Speed").
- ownership: OWNERSHIP=OWNED -> `Private`, SHARED -> `Shared`.
Order rows by the sentinel monitor device order (DEVn_ID) when it is known, otherwise by file order.

## [section:parameter_interface] Parameter Interface
Sources (all on the EM class): ATTRIBUTE and ATTRIBUTE_INSTANCE of every EM_ parameter and of
A_COMMAND, A_PV, FAILURE, MY_TAG, OWNER_ID; and the slot instances OPnnn_VALUE, OPnnn_DESC,
OPnnn_MIN, OPnnn_MAX, TPnnn_VALUE, TPnnn_DESC, TPnnn_MIN, TPnnn_MAX, RPnnn_VALUE, RPnnn_DESC.
List the parameters other entities (phases, operators, recipes) use to talk to the EM:
1. Every class-specific parameter declared on the EM class (by convention prefixed `EM_`).
2. The standard interface parameters A_COMMAND, A_PV, FAILURE, MY_TAG and OWNER_ID.
Do NOT list framework parameters (OPnnn_*, TPnnn_*, RPnnn_*, DEVn_ID, MSG*, PROMPT_*, OAR,
monitor, ownership-arbitration or internal scratch parameters such as SCALE_FACTOR unless
they are EM_ parameters).
Columns:
- description: if the parameter is referenced by an `OPnnn_VALUE`, `TPnnn_VALUE` or
  `RPnnn_VALUE` instance (`VALUE { REF="<param>" }`), use the matching `OPnnn_DESC` /
  `TPnnn_DESC` / `RPnnn_DESC` string. Otherwise use the standard description (see PCSD
  conventions) or expand the name into words (EM_PRSCTL_SIP_SP -> "Pressure Control SIP Setpoint").
- parameter_number: the referencing slot (`OP007`, `TP002`, `RP004`), else `N/A`.
- type: Floating point, Boolean, String, External Reference, or "Named Set: " followed by the
  actual set NAME from the instance VALUE (e.g. "Named Set: EM_MIXER" or "Named Set: _YES_NO").
- min / max: the `OPnnn_MIN` / `OPnnn_MAX` (or TP) values of the slot; `N/A` for RP slots
  and for parameters without a slot.
- default: the instance CV or STRING_VALUE; `<Blank>` for external references with no value.
Sort rows alphabetically by name.

## [section:alarms] Alarms
Sources: ATTRIBUTE_INSTANCE children of the EM class whose content contains PRIORITY_NAME.
One row per alarm parameter of the EM class: an ATTRIBUTE_INSTANCE whose VALUE contains
PRIORITY_NAME (typically names ending in `_ALM` or `BYPASS_ON`).
- alarm_name = the parameter NAME; priority = PRIORITY_NAME; enabled = ENAB (T/F);
  inverted = INV (T/F); alarm_type = ATYP.
- alarm = a readable name ("Fail Alarm", "Module Alarm", "Bypass Alarm").
- function = what raises the alarm. Use the standard PCSD function text when the alarm is a
  standard one; otherwise describe ALMATTR (the parameter that triggers it).

## [section:history] Historical Data Collection
Sources: ATTRIBUTE_INSTANCE children of the EM class whose content contains HISTORY_DATA_POINT.
One row per `HISTORY_DATA_POINT` on an EM-class parameter instance.
- value_recorded = `<parameter NAME>.<FIELD>` (e.g. `A_COMMAND.CV`).
- enable = Yes/No from ENABLED. display_representation = DATA_REPRESENTATION.
  data_characteristic = DATA_CHARACTERISTIC (keep upper case as in the source).
- sampling_period = SAMPLE_PERIOD_SECONDS + " sec" (e.g. `60 sec`).
- deviation = DEVIATION_LIMIT_FOR_COMPRESSION.
- at_least = RECORD_AT_LEAST_EVERY_MINUTES converted to hours when divisible by 60 (`240` -> `4 hr`), else `<n> min`.

## [section:hold_monitors] Hold Monitors
Sources: the `HMnn/DESC` instances inside the fail-monitor composite's definition (reachable
with a deep selector from the EM class).
Exactly 16 rows, numbers `01`..`16`. The EM's fail-monitor composite (often a block named
MONITOR) contains condition blocks HM01..HM16. description = the `HMnn/DESC` string of that
slot. When DESC is empty or "Not Used", write `Not Used`.

## [section:sentinel_monitors] Sentinel Monitors
Sources (both are required): (1) the sentinel monitor CALC block (e.g. SM_1_16, inside the
SENTINEL_MONITOR composite of the fail monitor; reachable with a deep selector from the EM class)
whose expression maps each slot to a device, and (2) every SFC ACTION whose expression writes
`SM_nn.CV' := TRUE`. DEVn_ID parameters only hold "Not Used" placeholders and do not name devices.
Exactly 16 rows, numbers `01`..`16`. The sentinel monitor calculation (a CALC block,
often SM_1_16 inside SENTINEL_MONITOR) has one condition per slot n that checks a device's
`/<DEVICE>/FAILURE.CV` and reports `DEVn_ID`.
- A slot is in use only if a command sequence enables it (writes `'^/MONITOR/SM_nn.CV' := TRUE`)
  AND the calculation checks a device for slot n. Write description `<DEVICE> Tag Name`
  (e.g. `AGITATOR Tag Name`).
- Slots with no condition, or whose SM_nn is never set TRUE by any command, are `Not Used`,
  even if a condition for them exists in the calculation.

## [section:command_sequences] Command sequence tables
One table per command (COMMAND_0000n). Walk the SFC from the INITIAL_STEP following
STEP_TRANSITION_CONNECTION / TRANSITION_STEP_CONNECTION. For each step emit:
- one row per ACTION, in action-name order (A0010, A0020, ..., EXIT last):
  step = `<step NAME> / <step DESCRIPTION>`, item = action NAME,
  delay = the delay condition in words, description = the action DESCRIPTION.
- then one row for the transition that leaves the step: item = transition NAME (T0010 or
  END), delay = `N/A`, description = transition DESCRIPTION.
Delay column:
- `N/A` when the action has `DELAY_TIME=0` or no delay expression.
- When the DELAY_EXPRESSION begins with `REM <text>`, use `<text>` (e.g. "Mode is Set",
  "Previous Action Complete", "Other Step Actions Complete").
- When it waits for other actions' STATE = Complete without a REM line, write
  `<first action> Complete` listing the awaited actions, e.g. "A0030, A0040, A0050 Complete".
- A non-zero DELAY_TIME becomes `<n> sec`.
If an action DESCRIPTION is empty, describe the action's EXPRESSION in a few words.

## [section:fbd_logic] Function Block Diagram Custom Logic
Sources: the FUNCTION_BLOCK children of the EM class (their `<BLOCK>/T_EXPRESSION` overrides are
attached automatically). Custom logic blocks are ACT/CND/CALC blocks; their definition is a
standard block type (ACT, CND, CALC), not a composite - do not confuse this section with Linked Composites.
One row per custom logic block on the EM class diagram: FUNCTION_BLOCKs of type ACT, CND or
CALC whose expression (the `<BLOCK>/T_EXPRESSION` override) is project-specific.
Exclude the standard PCSD ownership/acquisition block (sets HR, O_ID, OWNED, OPER_OWNED,
OWNED_SAVE), command composites, the MONITOR composite and plain gates (AND, NOT, OND, PDE, BFI, BFO).
- block = NAME; type = DEFINITION (ACT/CND/CALC).
- Account for every EM-level block that has its own expression: either list it, or name it in
  "note" with the reason it is excluded (e.g. "ACT1 excluded: standard PCSD ownership logic.").
- description = concise plain-English logic: for CND "TRUE if ..."; for ACT the parameters it
  writes and when ("Disable SPEED and FLOW LO, LOLO, and HI alarms."). Refer to parameters by name.

## [section:linked_composites] Linked Composites
Composite FUNCTION_BLOCKs directly on the EM class whose DEFINITION is a named library
composite (not `__hex__`) and is not part of the standard PCSD EM framework. Standard PCSD
framework composites (fail monitor, hold monitor, sentinel monitor, OAR, cancel wait) are
not listed. If none, return no rows and note "There are no linked composites used in this
Equipment Module Class."

## [section:embedded_composites] Embedded Composites
Composite FUNCTION_BLOCKs directly on the EM class with an embedded (`__hex__`) definition,
excluding the standard command composites (COMMAND_0000n) and the standard fail monitor
composite (MONITOR). If none, return no rows and note "There are no embedded composites used
in this Equipment Module Class."

## [section:operator_interfaces] Operator Interfaces
Sources: the EM class header (INSTRUMENT_AREA_DISPLAY, DETAIL_DISPLAY).
Three short subsections: Faceplate Interface, Detail Faceplate Interface and Dynamo Interface.
When the class uses standard PCSD displays (display names start with `PCSD_`), each subsection is
exactly the standard sentence from the PCSD conventions, e.g. "The standard PCSD Faceplate
Interface is used. Refer to PCSD Books Online for details regarding this interface." The display
name may follow in a second sentence (e.g. "Display: PCSD_EM_120_FP."). Never put the display
name in place of the interface name.
