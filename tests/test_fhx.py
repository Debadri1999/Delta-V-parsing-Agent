from __future__ import annotations

from dds_agent.fhx.source import FhxSource, Selector, SourceBundle, render
from dds_agent.fhx.tree import parse_fhx, read_fhx_text, tokenize


def test_tokenizer_handles_escapes_comments_and_split_literals():
    toks = tokenize('A="x ""q"" y" /* gone */ B="one" "two" C=3')
    values = [t.value for t in toks]
    assert 'x "q" y' in values
    assert "gone" not in " ".join(values)
    nodes_props = parse_fhx('X { B="one" "two" }')[0].props
    assert nodes_props["B"] == "onetwo"


def test_tree_parses_nested_blocks(mini_text):
    nodes = parse_fhx(mini_text)
    em = next(n for n in nodes if n.name == "_EM-MINI-CL")
    assert em.get("PERIOD") == "1"
    assert [c.name for c in em.children if c.keyword == "MODULE_BLOCK"] == ["DRN_VLV"]


def test_read_fhx_text_decodes_utf16(tmp_path):
    p = tmp_path / "x.fhx"
    p.write_bytes('MODULE_CLASS NAME="A" { }'.encode("utf-16"))
    assert read_fhx_text(p).startswith("MODULE_CLASS")


def test_render_drops_layout_noise(mini_source: FhxSource):
    text = render(mini_source.top["__AAAA0000_00000001__"])
    assert "RECTANGLE" not in text and "BOX_GRAPHIC" not in text
    assert "ACTION NAME=A0010" in text


def test_root_block_is_the_unreferenced_module(mini_source: FhxSource):
    assert mini_source.root_blocks("MODULE_CLASS") == ["_EM-MINI-CL"]


def test_scoped_overrides_attach_to_their_block(mini_source: FhxSource):
    nodes = mini_source.select(Selector(scope="_EM-MINI-CL", keyword="FUNCTION_BLOCK", name_regex="^SIP$"))
    assert len(nodes) == 1 and "SIP/T_EXPRESSION" in nodes[0].text


def test_follow_references_pulls_definition(mini_source: FhxSource):
    nodes = mini_source.select(Selector(scope="_EM-MINI-CL", keyword="FUNCTION_BLOCK", name_regex="COMMAND_00000",
                                        follow_references=True))
    assert [n.ref for n in nodes][-1] == "__AAAA0000_00000001__::FUNCTION_BLOCK_DEFINITION"


def test_deep_selection_enters_block_definitions(mini_source: FhxSource):
    nodes = mini_source.select(Selector(scope="_EM-MINI-CL", keyword="ATTRIBUTE_INSTANCE",
                                        name_regex=r"^HM\d\d/DESC$", deep=True))
    assert [n.ref for n in nodes] == ["_EM-MINI-CL::FUNCTION_BLOCK:MONITOR/ATTRIBUTE_INSTANCE:HM01/DESC"]


def test_text_regex_filters_on_content(mini_source: FhxSource):
    alarms = mini_source.select(Selector(scope="_EM-MINI-CL", keyword="ATTRIBUTE_INSTANCE", text_regex="PRIORITY_NAME"))
    assert [n.ref.split(":")[-1] for n in alarms] == ["FAIL_ALM"]
    actions = mini_source.select(Selector(scope="*", keyword="ACTION", deep=True, text_regex=r"SM_\d\d\.CV' := TRUE"))
    assert len(actions) == 1


def test_search_finds_identifiers(mini_source: FhxSource):
    hits = mini_source.search("OP001_DESC drain time", 3)
    assert any("OP001_DESC" in h.item.id for h in hits)


def test_outline_lists_embedded_definitions(mini_source: FhxSource):
    outline = mini_source.outline(focus="_EM-MINI-CL")
    assert "embedded definition used by _EM-MINI-CL" in outline and "HM01/DESC" in outline


def test_bundle_keeps_refs(mini_source: FhxSource):
    bundle = SourceBundle.from_nodes(mini_source.select(Selector(scope="_EM-MINI-CL", keyword="MODULE_BLOCK")))
    assert bundle.refs == ["_EM-MINI-CL::MODULE_BLOCK:DRN_VLV"] and "### SOURCE [" in bundle.text
