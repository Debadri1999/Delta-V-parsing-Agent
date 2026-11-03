# PCSD (Emerson standard library) conventions

These conventions hold for every command-driven EM class built on the PCSD library.

## [section:parameter_interface] Standard interface parameter descriptions
- A_COMMAND - "Equipment Module Command" (type "Named Set: " + the EM's command set NAME, default Undefined).
- A_PV - "Equipment Module PV" (type "Named Set: " + the EM's command set NAME, default Undefined).
- FAILURE - "Equipment Module Failure" (Boolean, default False).
- MY_TAG - "Equipment Module Instance Tag Name" (String, default "EM Name").
- OWNER_ID - "Equipment Module Owner ID" (String, default "(None)").
Parameter-name abbreviations used in EM_ names: SP setpoint, PV process value, TM time,
TMP temperature, PRES/PRS pressure, TOUT timeout, DB deadband, REQ request, RSULT result,
STB stabilization, SELTST seal test, PURG purge, AGIT agitator, SPD speed, LIM limit,
LO low, HI high, INITL initial, DIFF differential, CTL/CTRL control, FAIL failure.

## [section:alarms] Standard PCSD alarms
| alarm_name | alarm | function |
|---|---|---|
| HORN_ALM | Horn Alarm | OAR action is ask-again and timeout occurs |
| PROMPT_ALM | Prompt Alarm | Prompt Active |
| MODULE_ALM | Module Alarm | The Equipment Module has its BAD_ACTIVE status bit set |
| BYPASS_ON | Bypass Alarm | Sentinel Monitor or Hold Monitor Bypass |
| FAIL_ALM | Fail Alarm | Sentinel Monitor Failure |
Order the standard alarms as listed above, then any project-specific alarms.

## [section:command_sequences] Common action descriptions
- "Common Hold Actions" = set hold messaging, disable all run commands and set TRANS to default.
- "Hold Logic Complete Actions" = set hold messaging, set HOLD_REQ to True and reset operator hold request.
- Delay REM phrases used across PCSD commands: "Mode is Set", "Previous Action Complete",
  "Other Step Actions Complete".
- COMMAND_00000 is always HOLD, the failsafe command.

## [section:sentinel_monitors] Sentinel monitor slots
The PCSD sentinel monitor provides 16 slots. Each used slot monitors one device's FAILURE
output and is labelled "<DEVICE> Tag Name" (the DEVn_ID parameter holds the instance tag).
Slots are enabled per command by `'^/MONITOR/SM_nn.CV' := TRUE` writes in the command sequences.

## [section:hold_monitors] Hold monitor slots
The PCSD hold monitor provides 16 condition slots HM01..HM16 inside the fail monitor composite.
HM01 is conventionally "Module Ownership Lost". Unused slots have DESC "Not Used" and expression False.

## [section:fbd_logic] Standard blocks that are not custom logic
The PCSD EM template always contains an ACT block that maintains ownership (writes HR, O_ID,
OWNED, OPER_OWNED, OWNED_SAVE from OWNER_ID and BFI1/OUT_INT). It is framework logic, not
custom logic, and is not listed. Boolean fan-in/out (BFI/BFO) and edge/timer/gate blocks
(PDE, OND, NOT, AND) are wiring, not custom logic.

## [section:linked_composites] PCSD framework composites
C_H_16MON120 (hold monitor), C_SNTL_16MON120 (sentinel monitor), C_OAR_ENG_EM120 (operator
prompts), C_CNL_WT_EM120 (cancel wait) live inside the MONITOR composite and are framework,
not project linked composites.

## [section:operator_interfaces] Standard PCSD operator interface text
- Faceplate Interface: "The standard PCSD Faceplate Interface is used. Refer to PCSD Books Online for details regarding this interface."
- Detail Faceplate Interface: "The standard PCSD Detail Faceplate Interface is used. Refer to PCSD Books Online for details regarding this interface."
- Dynamo Interface: "The standard PCSD Dynamo Interface is used. Refer to PCSD Books Online for details regarding this interface."
