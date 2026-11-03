"""Deterministic guardrails over LLM-produced sections.

Guardrails never generate content; they verify it. Errors block release and are
fed back to the parser agent; warnings are reported to the reviewing engineer.

* **Schema** - rows the parser could not validate.
* **Grounding** - fields that must be copied verbatim (names, classes, priorities,
  item ids) must occur literally in the FHX source.
* **Coverage** - structural items present in the source (module blocks, alarm
  parameters, history points, SFC actions/transitions) must all be documented.
* **Shape** - fixed row counts, unique keys, required properties.
* **Cross-section consistency** - e.g. sentinel devices must be documented devices.

When a check proves that something exists in the export but was not in the agent's
source, it also returns ``fetch`` selectors; the orchestrator adds those blocks to the
source for the next iteration, so a weak plan cannot cap quality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from dds_agent.fhx.source import FhxSource, Selector, SourceBundle
from dds_agent.fhx.tree import FhxNode
from dds_agent.schema.dds import SectionSpec

SKIP_GROUNDING = {"", "N/A", "NA", "T", "F", "TRUE", "FALSE", "YES", "NO", "0", "1"}
REQUIRED_PROPERTIES = ("Named Set", "Description", "Scan Rate (sec)", "Primary Control", "Detail Display",
                       "Faceplate Display")


@dataclass
class GuardrailReport:
    """Findings for one section (or one command)."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fetch: list[Selector] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "GuardrailReport") -> None:
        self.errors += other.errors
        self.warnings += other.warnings
        self.fetch += other.fetch

    def render(self) -> str:
        lines = [f"ERROR: {e}" for e in self.errors] + [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


@dataclass
class GuardrailContext:
    """Facts the checks need about the run."""

    source: FhxSource
    main_module: str
    command_set: str = ""

    @property
    def main(self) -> FhxNode | None:
        return self.source.top.get(self.main_module)


def _children(node: FhxNode | None, keyword: str) -> list[FhxNode]:
    return [c for c in node.children if c.keyword == keyword] if node else []


def _value_fields(instance: FhxNode) -> dict:
    """Fields of an instance's VALUE, written either as ``VALUE { }`` block or ``VALUE= { }`` struct."""
    if isinstance(instance.props.get("VALUE"), dict):
        return instance.props["VALUE"]
    block = next((c for c in instance.children if c.keyword == "VALUE"), None)
    return block.props if block is not None else {}


_PLACEHOLDER_RE = re.compile(r"<(?!blank>|null>)[A-Za-z][^<>]{0,40}>", re.I)
_SLOT_RE = re.compile(r"^(OP|TP|RP)(\d{3})_VALUE$")
_SM_ENABLE_RE = re.compile(r"SM_(\d\d)\.CV'\s*:=\s*TRUE", re.I)
_ABSENCE_RE = re.compile(r"not (found|present|available|in (the )?source)|does not exist|missing|absent", re.I)


def _placeholders(spec: SectionSpec, rows: list[dict]) -> GuardrailReport:
    """Template text (``<SET>``, ``<name>``) copied into the output instead of a real value."""
    rep = GuardrailReport()
    for row in rows:
        for k, v in row.items():
            if isinstance(v, str) and _PLACEHOLDER_RE.search(v):
                rep.errors.append(f"{row.get(spec.key, '?')}.{k}={v!r} contains template text, not a source value")
    return rep


def _num(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _slots(main: FhxNode | None) -> dict[str, tuple[str, str | None, str | None]]:
    """Parameter -> (slot, min, max) for parameters referenced by OPnnn/TPnnn/RPnnn_VALUE instances."""
    inst = {c.name: c for c in _children(main, "ATTRIBUTE_INSTANCE")}
    out: dict[str, tuple[str, str | None, str | None]] = {}
    for name, node in inst.items():
        m = _SLOT_RE.match(name)
        ref = _value_fields(node).get("REF") if m else None
        if m and ref:
            slot = f"{m.group(1)}{m.group(2)}"
            lo = _value_fields(inst[f"{slot}_MIN"]).get("CV") if f"{slot}_MIN" in inst else None
            hi = _value_fields(inst[f"{slot}_MAX"]).get("CV") if f"{slot}_MAX" in inst else None
            out[str(ref)] = (slot, lo, hi)
    return out


def _parameter_slots(rows: list[dict], ctx: GuardrailContext) -> GuardrailReport:
    rep = GuardrailReport()
    slots = _slots(ctx.main)
    wrong: set[str] = set()
    for r in rows:
        name, number = r.get("name", ""), str(r.get("parameter_number", "")).strip()
        if name in slots:
            slot, lo, hi = slots[name]
            if number != slot:
                rep.errors.append(f"{name}.parameter_number={number!r}, but {slot}_VALUE references {name}")
                wrong.add(slot)
            for field_name, want in (("min", lo), ("max", hi)):
                if want is not None and _num(r.get(field_name)) != _num(want):
                    rep.errors.append(f"{name}.{field_name}={r.get(field_name)!r}, but {slot}_{field_name.upper()} is {want}")
                    wrong.add(slot)
        elif number not in ("", "N/A"):
            rep.errors.append(f"{name}.parameter_number={number!r}, but no slot VALUE references {name}")
    if wrong:  # give the agent the slot instances it evidently did not see
        rep.fetch.append(Selector(scope=ctx.main_module, keyword="ATTRIBUTE_INSTANCE",
                                  name_regex="^(" + "|".join(sorted(wrong)) + ")_(VALUE|DESC|MIN|MAX)$"))
    return rep


def _mentions(text: str, name: str) -> bool:
    """``name`` appears in ``text`` as a whole identifier."""
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text) is not None


def _names_selector(scope: str, keyword: str, names: list[str]) -> Selector:
    return Selector(scope=scope, keyword=keyword, name_regex="^(" + "|".join(re.escape(n) for n in names) + ")$")


def _norm_value(v: object) -> str:
    s = str(v if v is not None else "").strip()
    n = _num(s)
    if n is not None:
        return repr(n)
    return {"t": "true", "f": "false"}.get(s.lower(), s.lower())


def _sentinel_enablement(rows: list[dict], ctx: GuardrailContext) -> GuardrailReport:
    """A slot is used iff some SFC action writes SM_nn := TRUE; a used slot must name a device."""
    rep = GuardrailReport()
    devices = [c.name for c in _children(ctx.main, "MODULE_BLOCK")]
    enabled: set[str] = set()
    for top in ctx.source.nodes:
        for n in top.walk():
            if n.keyword == "ACTION":
                enabled |= set(_SM_ENABLE_RE.findall(str(n.get("EXPRESSION", ""))))
    if not enabled:
        return rep
    for r in rows:
        number = str(r.get("number", "")).zfill(2)
        desc = str(r.get("description", "")).strip()
        unused = desc.lower() in ("not used", "")
        if number in enabled and unused:
            rep.errors.append(f"sentinel slot {number} is enabled by a command (SM_{number} := TRUE) but marked Not Used")
        elif number not in enabled and not unused:
            rep.errors.append(f"sentinel slot {number} is never enabled by any command but is not marked Not Used")
        elif number in enabled and devices and not any(_mentions(desc, d) for d in devices):
            rep.errors.append(f"sentinel slot {number} is in use but its description {desc!r} names no device; read the "
                              "sentinel calculation that maps slots to device FAILURE outputs")
    if any("names no device" in e for e in rep.errors):
        rep.fetch.append(Selector(scope=ctx.main_module, keyword="FUNCTION_BLOCK", deep=True, text_regex=r"DEV\d+_ID"))
    return rep


def _fbd_candidates(rows: list[dict], note: str, ctx: GuardrailContext) -> GuardrailReport:
    """Every EM-level block with its own expression must be documented or explicitly excluded in the note."""
    rep = GuardrailReport()
    main = ctx.main
    if main is None:
        return rep
    scoped = {c.name.split("/", 1)[0] for c in _children(main, "ATTRIBUTE_INSTANCE") if c.name.endswith("/T_EXPRESSION")}
    candidates = [c.name for c in _children(main, "FUNCTION_BLOCK") if c.name in scoped
                  and getattr(ctx.source.top.get(str(c.attrs.get("DEFINITION"))), "keyword", "") != "FUNCTION_BLOCK_DEFINITION"]
    documented = {r.get("block") for r in rows}
    excluded = [c for c in candidates if c not in documented and re.search(rf"\b{re.escape(c)}\b", note)]
    unaccounted = [c for c in candidates if c not in documented and c not in excluded]
    hint = (f" They exist on {ctx.main_module}; if they are not in your SOURCE, request them with "
            "need_more_context (e.g. their block names).")
    if unaccounted or (excluded and _ABSENCE_RE.search(note)):
        rep.fetch.append(_names_selector(ctx.main_module, "FUNCTION_BLOCK", unaccounted + excluded))
    if unaccounted:
        rep.errors.append("blocks with their own expression are neither documented nor excluded with a reason in "
                          f"the note: {', '.join(unaccounted)}." + hint)
    if excluded and _ABSENCE_RE.search(note):
        rep.errors.append(f"the note claims blocks are absent, but these exist with expressions: {', '.join(excluded)}."
                          + hint)
    elif excluded:
        rep.warnings.append(f"excluded from custom logic (confirm): {', '.join(excluded)}")
    return rep


def _grounding(spec: SectionSpec, rows: list[dict], bundle: SourceBundle, ctx: GuardrailContext) -> GuardrailReport:
    rep = GuardrailReport()
    for row in rows:
        key = row.get(spec.key, "?")
        for f in spec.grounded:
            value = str(row.get(f, "")).strip()
            if value.upper() in SKIP_GROUNDING:
                continue
            if value in bundle.text:
                continue
            if value in ctx.source.flat_text:
                rep.warnings.append(f"{key}.{f}={value!r} is in the FHX but not in the planned source blocks")
            else:
                rep.errors.append(f"{key}.{f}={value!r} does not occur anywhere in the FHX export (ungrounded)")
    return rep


def _shape(spec: SectionSpec, rows: list[dict]) -> GuardrailReport:
    rep = GuardrailReport()
    keys = [str(r.get(spec.key, "")) for r in rows]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes and spec.kind == "table":
        rep.errors.append(f"duplicate {spec.key} values: {', '.join(dupes)}")
    if spec.fixed_count is not None:
        expected = [f"{i:02d}" for i in range(1, spec.fixed_count + 1)]
        got = [k.zfill(2) for k in keys]
        if got != expected:
            missing = [e for e in expected if e not in got]
            rep.errors.append(f"expected rows {expected[0]}..{expected[-1]} in order; got {len(rows)} rows"
                              + (f", missing {', '.join(missing)}" if missing else ""))
    return rep


def _coverage(spec: SectionSpec, rows: list[dict], ctx: GuardrailContext, note: str = "",
              bundle: SourceBundle | None = None) -> GuardrailReport:
    rep = GuardrailReport()
    in_source = set(bundle.refs) if bundle else set()
    main = ctx.main
    got = {str(r.get(spec.key, "")) for r in rows}
    if spec.id == "control_module_interface":
        need = [c.name for c in _children(main, "MODULE_BLOCK")]
        missing = [n for n in need if n not in got]
        if missing:
            rep.errors.append(f"module blocks not documented: {', '.join(missing)}")
            rep.fetch.append(_names_selector(ctx.main_module, "MODULE_BLOCK", missing))
        extra = sorted(got - set(need))
        if extra:
            rep.errors.append(f"devices that are not module blocks of {ctx.main_module}: {', '.join(extra)}")
    elif spec.id == "alarms":
        need = [c.name for c in _children(main, "ATTRIBUTE_INSTANCE") if "PRIORITY_NAME" in _value_fields(c)]
        missing = [n for n in need if n not in got]
        if missing:
            rep.errors.append(f"alarm parameters not documented: {', '.join(missing)}")
            rep.fetch.append(_names_selector(ctx.main_module, "ATTRIBUTE_INSTANCE", missing))
    elif spec.id == "history":
        need = [f"{c.name}.{h.attrs.get('FIELD', 'CV')}" for c in _children(main, "ATTRIBUTE_INSTANCE")
                for h in _children(c, "HISTORY_DATA_POINT")]
        missing = [n for n in need if n not in got]
        if missing:
            rep.errors.append(f"history points not documented: {', '.join(missing)}")
            rep.fetch.append(_names_selector(ctx.main_module, "ATTRIBUTE_INSTANCE", [m.rsplit(".", 1)[0] for m in missing]))
    elif spec.id == "parameter_interface":
        need = [c.name for c in _children(main, "ATTRIBUTE") if c.name.startswith("EM_")]
        missing = [n for n in need if n not in got]
        if missing:
            rep.errors.append(f"EM_ parameters not listed: {', '.join(missing)}")
        instances = {c.name: c for c in _children(main, "ATTRIBUTE_INSTANCE")}
        for r in rows:  # defaults must equal the configured instance value
            fields = _value_fields(instances[r["name"]]) if r.get("name") in instances else {}
            want = fields.get("CV", fields.get("STRING_VALUE"))
            if want is not None and str(want).strip() and _norm_value(want) != _norm_value(r.get("default")):
                rep.errors.append(f"{r['name']}.default={r.get('default')!r}, but the instance value is {want!r}")
        unseen = [n for n in sorted(set(need) | got) if n in instances
                  and f"{ctx.main_module}::ATTRIBUTE_INSTANCE:{n}" not in in_source]
        if missing or unseen:
            names = sorted(set(missing) | set(unseen))
            rep.fetch += [_names_selector(ctx.main_module, "ATTRIBUTE", names),
                          _names_selector(ctx.main_module, "ATTRIBUTE_INSTANCE", names),
                          Selector(scope=ctx.main_module, keyword="ATTRIBUTE_INSTANCE",
                                   name_regex=r"^(OP|TP|RP)\d{3}_(VALUE|DESC|MIN|MAX)$")]
        rep.extend(_parameter_slots(rows, ctx))
    elif spec.id == "sentinel_monitors":
        rep.extend(_sentinel_enablement(rows, ctx))
    elif spec.id == "fbd_logic":
        rep.extend(_fbd_candidates(rows, note, ctx))
    elif spec.id == "properties":
        missing = [p for p in REQUIRED_PROPERTIES if p not in got]
        if missing:
            rep.errors.append(f"missing properties: {', '.join(missing)}")
        named = next((r.get("value") for r in rows if r.get("property") == "Named Set"), None)
        if ctx.command_set and named and named != ctx.command_set:
            rep.errors.append(f"Named Set {named!r} differs from the command set {ctx.command_set!r}")
    return rep


def check_section(spec: SectionSpec, output: dict, bundle: SourceBundle, ctx: GuardrailContext,
                  invalid_rows: list[str] | None = None) -> GuardrailReport:
    """Run all guardrails for a non-command section."""
    rep = GuardrailReport()
    for msg in invalid_rows or []:
        rep.errors.append(f"schema: {msg}")
    if spec.kind == "text":
        if not str(output.get("text", "")).strip():
            rep.errors.append("section text is empty")
        return rep
    rows = output.get("rows", [])
    rep.extend(_shape(spec, rows))
    rep.extend(_placeholders(spec, rows))
    rep.extend(_grounding(spec, rows, bundle, ctx))
    rep.extend(_coverage(spec, rows, ctx, str(output.get("note", "")), bundle))
    if not rows and spec.id not in ("linked_composites", "embedded_composites") and not output.get("note"):
        rep.errors.append("table is empty and has no explanatory note")
    return rep


def _sfc_items(definition: FhxNode | None) -> tuple[dict[str, list[str]], list[str]]:
    """Step -> action names, and transition names, from an SFC definition."""
    steps: dict[str, list[str]] = {}
    transitions: list[str] = []
    if definition is None:
        return steps, transitions
    for n in definition.walk():
        if n.keyword == "STEP":
            steps[n.name] = [a.name for a in n.children if a.keyword == "ACTION"]
        elif n.keyword == "TRANSITION":
            transitions.append(n.name)
    return steps, transitions


def check_command(spec: SectionSpec, output: dict, bundle: SourceBundle, ctx: GuardrailContext, block: str,
                  definition: str, invalid_rows: list[str] | None = None) -> GuardrailReport:
    """Guardrails for one command sequence table."""
    rep = GuardrailReport()
    for msg in invalid_rows or []:
        rep.errors.append(f"schema: {msg}")
    rows = output.get("rows", [])
    rep.extend(_placeholders(spec, rows))
    if output.get("block") != block:
        rep.errors.append(f"block is {output.get('block')!r}, expected {block!r}")
    m = re.search(r"(\d+)$", block)
    enum = ctx.source.top.get(ctx.command_set)
    if m and enum is not None:
        want = next((e.name for e in _children(enum, "ENTRY") if str(e.attrs.get("VALUE")) == str(int(m.group(1)))), None)
        if want and output.get("command") != want:
            rep.errors.append(f"command name is {output.get('command')!r}; named set {ctx.command_set} says {want!r}")
        if output.get("value") != int(m.group(1)):
            rep.errors.append(f"value is {output.get('value')!r}, expected {int(m.group(1))}")
    steps, transitions = _sfc_items(ctx.source.top.get(definition))
    documented = {(str(r.get("step", "")).split("/")[0].strip(), str(r.get("item", "")).strip()) for r in rows}
    missing_actions = [f"{s}/{a}" for s, acts in steps.items() for a in acts if (s, a) not in documented]
    if missing_actions:
        rep.errors.append(f"actions not documented: {', '.join(missing_actions)}")
    items = {i for _, i in documented}
    missing_tr = [t for t in transitions if t not in items]
    if missing_tr:
        rep.errors.append(f"transitions not documented: {', '.join(missing_tr)}")
    known = {f"{s}/{a}" for s, acts in steps.items() for a in acts} | set(transitions)
    unknown = sorted(f"{s}/{i}" for s, i in documented if f"{s}/{i}" not in known and i not in transitions)
    if unknown:
        rep.errors.append(f"items that do not exist in the SFC: {', '.join(unknown)}")
    step_desc = {}
    if definition in ctx.source.top:
        step_desc = {n.name: str(n.get("DESCRIPTION", "")) for n in ctx.source.top[definition].walk() if n.keyword == "STEP"}
    for r in rows:
        parts = [p.strip() for p in str(r.get("step", "")).split("/", 1)]
        if len(parts) == 2 and parts[0] in step_desc and step_desc[parts[0]] and parts[1] != step_desc[parts[0]]:
            rep.warnings.append(f"step {parts[0]} label {parts[1]!r} differs from DESCRIPTION {step_desc[parts[0]]!r}")
            break
    return rep


def check_document(sections: dict[str, dict], ctx: GuardrailContext) -> GuardrailReport:
    """Cross-section consistency checks."""
    rep = GuardrailReport()
    devices = {r.get("device") for r in sections.get("control_module_interface", {}).get("rows", [])}
    for r in sections.get("sentinel_monitors", {}).get("rows", []):
        desc = str(r.get("description", ""))
        if desc.endswith("Tag Name"):
            dev = desc[: -len("Tag Name")].strip()
            if devices and dev not in devices:
                rep.warnings.append(f"sentinel monitor {r.get('number')} monitors {dev!r}, which is not a documented device")
    numbers = [r.get("parameter_number") for r in sections.get("parameter_interface", {}).get("rows", [])
               if r.get("parameter_number") not in (None, "", "N/A")]
    dupes = sorted({n for n in numbers if numbers.count(n) > 1})
    if dupes:
        rep.errors.append(f"parameter numbers used twice: {', '.join(dupes)}")
    return rep
