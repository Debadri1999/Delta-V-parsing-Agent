# FHX primer

## [section:*] FHX syntax and building blocks
An FHX file is a DeltaV configuration export made of nested blocks
`KEYWORD ATTR=value { PROP=value ... CHILD_KEYWORD NAME="..." { ... } }`.
`T` / `F` mean True / False. Strings are quoted; `""` inside a string is a quote.
Sources shown to you are compacted: layout (rectangles, graphics, wires) has been removed.

Typical top-level blocks:
- `MODULE_CLASS` - a class definition. The equipment module (EM) class to document is the
  module class that no other block references (refs=0 in the outline), usually with a
  category under `Equipment Module Classes`. Other MODULE_CLASS blocks in the file are the
  control-module classes the EM uses (valves, motors, analog inputs).
- `FUNCTION_BLOCK_DEFINITION` - a composite definition. Names like `__1A2B3C4D_5E6F7A8B__`
  (double underscores + hex) are *embedded* composites owned by one block; named ones
  (`C_H_16MON120`) are *linked* library composites.
- `FUNCTION_BLOCK_TEMPLATE` - a standard DeltaV block type (ACT, CND, CALC, AND, NOT, OND...).
- `ENUMERATION_SET` - a named set: `ENTRY VALUE=n NAME="..."`.

Inside a module class:
- `MODULE_BLOCK NAME=X MODULE=CLASS { DESCRIPTION OWNERSHIP }` - an embedded control module (a device).
- `FUNCTION_BLOCK NAME=X DEFINITION=D` - a function block of type/definition D.
- `ATTRIBUTE NAME=X TYPE=T { CONNECTION GROUP ... }` - a parameter declaration.
- `ATTRIBUTE_INSTANCE NAME=X { VALUE { ... } }` - the configured value of parameter X.
  `NAME="BLOCK/PARAM"` is an override of PARAM inside block BLOCK (path-scoped).
- `COMMAND_DRIVEN_ALGORITHM { COMMAND_SET=S }` - the EM is command driven; S is the
  named set listing its commands.

Values:
- `VALUE { CV=v }` - current value v. `VALUE { CV="text" }` - string value.
- `VALUE { SET=S STRING_VALUE=v }` - named-set value v of set S.
- `VALUE { REF="X" }` - an internal reference to parameter X.
- `VALUE { TYPE=EXPRESSION|CONDITION EXPRESSION="..." }` - Structured Text logic.
- Alarm values: `VALUE { PRIORITY_NAME ENAB INV ATYP ALMATTR PARAM1 PARAM2 ... }`.
- History: `HISTORY_DATA_POINT FIELD="CV" { ENABLED SAMPLE_PERIOD_SECONDS RECORD_AT_LEAST_EVERY_MINUTES
  DEVIATION_LIMIT_FOR_COMPRESSION DATA_CHARACTERISTIC DATA_REPRESENTATION }` inside an ATTRIBUTE_INSTANCE.

Type keywords: FLOAT = Floating point, BOOLEAN = Boolean, UNICODE_STRING = String,
ENUMERATION_VALUE = Named Set, EXTERNAL_REFERENCE = External Reference,
INTERNAL_REFERENCE = Internal Reference, UINT8/16/32 = 8/16/32 bit unsigned integer,
INT16/32 = 16/32 bit signed integer.

## [section:command_sequences] SFC command composites
In a command-driven EM each command is a `FUNCTION_BLOCK NAME="COMMAND_0000n"` whose
definition (an embedded `FUNCTION_BLOCK_DEFINITION`) holds an `SFC_ALGORITHM`:
- `STEP NAME="S0010" { DESCRIPTION="..." ACTION NAME="A0010" { DESCRIPTION EXPRESSION
  DELAY_TIME | DELAY_EXPRESSION CONFIRM_EXPRESSION } ... }`
- `INITIAL_STEP="S0000"` marks the start step.
- `TRANSITION NAME="T0010" { DESCRIPTION EXPRESSION TERMINATION }`; `END` (TERMINATION=T) ends the command.
- `STEP_TRANSITION_CONNECTION STEP=S TRANSITION=T` - transition T follows step S.
- `TRANSITION_STEP_CONNECTION TRANSITION=T STEP=S` - step S follows transition T.
The command NAME (HOLD, CLOSED, ...) is the COMMAND_SET entry whose VALUE equals the
number in the block name (`COMMAND_00002` -> VALUE=2).
The command's failure maximum time is the `CFM_MAX_TIME` value of the command block
(an `ATTRIBUTE_INSTANCE NAME="COMMAND_0000n/CFM_MAX_TIME"` override or the definition's
default). If no value is configured it is 0.
