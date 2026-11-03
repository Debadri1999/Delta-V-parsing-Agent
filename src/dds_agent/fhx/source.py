"""The agents' view of an FHX file: compact rendering, outline, selectors and retrieval.

Everything here is structural and format-generic:

* **Rendering** drops layout noise (graphics, rectangles, IDs) and nothing else.
* **Chunks** are the children of each top-level block. Path-scoped overrides
  (``ACT2/T_EXPRESSION``) are attached to the sibling block they are scoped to.
* **References** are any attribute whose value equals another top-level block's
  NAME (``DEFINITION=``, ``MODULE=``, ``COMMAND_SET=`` ...). Following them lets
  an agent pull in a block's definition without knowing what the keyword means.
* **Outline** summarizes the file for the cartographer agent.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from dds_agent.fhx.tree import FhxNode, parse_fhx, read_fhx_text
from dds_agent.rag.retriever import Hit, TfidfIndex

logger = logging.getLogger(__name__)

# Layout and bookkeeping that carry no configuration meaning.
NOISE_KEYWORDS: frozenset[str] = frozenset(
    {"GRAPHICS", "BOX_GRAPHIC", "TEXT_GRAPHIC", "WIRE", "SEGMENT", "CATEGORY", "EXPOSED_PARAMETER", "EXTENSIBLE_ATTRIBUTE"}
)
NOISE_PROPS: frozenset[str] = frozenset({"RECTANGLE", "POSITION", "ORIGIN", "END", "ID", "HELP_ID"})
NOISE_ATTRS: frozenset[str] = frozenset({"user", "time"})
MAX_CHUNK_CHARS = 6000


def _quote(value: Any) -> str:
    if isinstance(value, dict):
        return "{ " + " ".join(f"{k}={_quote(v)}" for k, v in value.items() if k != "_flags") + " }"
    if isinstance(value, list):
        return " ".join(_quote(v) for v in value)
    s = str(value)
    return s if re.fullmatch(r"[A-Za-z0-9_.\-+$]+", s) else '"' + s.replace('"', '""') + '"'


def render(node: FhxNode, indent: int = 0) -> str:
    """Re-serialize a node as compact FHX without layout noise."""
    pad = "  " * indent
    header = " ".join([node.keyword] + [f"{k}={_quote(v)}" for k, v in node.attrs.items() if k not in NOISE_ATTRS])
    body: list[str] = []
    for key, value in node.props.items():
        if key in NOISE_PROPS:
            continue
        for v in value if isinstance(value, list) else [value]:
            body.append(f"{pad}  {key}={_quote(v)}")
    for child in node.children:
        if child.keyword not in NOISE_KEYWORDS:
            body.append(render(child, indent + 1))
    if not body:
        return pad + header
    if len(body) <= 2 and all("\n" not in b for b in body) and sum(len(b) for b in body) < 120:
        return pad + header + " { " + " ".join(b.strip() for b in body) + " }"
    return "\n".join([pad + header, pad + "{", *body, pad + "}"])


@dataclass
class Chunk:
    """A retrievable piece of the FHX file.

    Attributes:
        id: Stable reference, ``<scope>::<KEYWORD>:<NAME>``. Used in source citations.
        scope: NAME of the top-level block the chunk belongs to.
        keyword: Keyword of the chunk's node.
        name: NAME of the chunk's node.
        text: Compact FHX text shown to agents.
        line: First source line.
    """

    id: str
    scope: str
    keyword: str
    name: str
    text: str
    line: int


class Selector(BaseModel):
    """Structural query the cartographer emits to point a section at source blocks.

    Attributes:
        scope: Top-level block NAME to search in; ``"*"`` searches all top-level blocks.
        keyword: Child keyword to match (``"*"`` = any). Use the top-level keyword with
            ``scope_is_target`` to select the top-level block itself.
        name_regex: Optional regex searched in the child NAME (re.search; anchor with ^ and $).
        text_regex: Optional regex searched in the node's rendered content (e.g. ``PRIORITY_NAME``).
        deep: Match descendants at any depth instead of direct children only.
        follow_references: Also include top-level blocks the matches reference.
        scope_is_target: Select the scope block itself (header only unless ``keyword`` matches children).
    """

    scope: str = "*"
    keyword: str = "*"
    name_regex: str | None = None
    text_regex: str | None = None
    deep: bool = False
    follow_references: bool = False
    scope_is_target: bool = False


@dataclass
class SelectedNode:
    """A node chosen by a selector, rendered for an agent."""

    ref: str
    text: str


class FhxSource:
    """Indexed FHX file.

    Args:
        nodes: Top-level nodes from :func:`parse_fhx`.
        path: Source path (for reporting).
    """

    def __init__(self, nodes: list[FhxNode], path: Path | None = None) -> None:
        self.path = path
        self.nodes = nodes
        self.top: dict[str, FhxNode] = {}
        for n in nodes:
            if n.name:
                self.top.setdefault(n.name, n)
        self.chunks: list[Chunk] = list(self._build_chunks())
        self.index: TfidfIndex[Chunk] = TfidfIndex(self.chunks, lambda c: f"{c.keyword} {c.name} {c.text}")
        self.referenced_by: dict[str, set[str]] = self._reference_graph()
        self._flat_text: str | None = None
        logger.info("Indexed %d top-level blocks into %d chunks", len(nodes), len(self.chunks))

    @classmethod
    def load(cls, path: Path) -> "FhxSource":
        """Read, parse and index an FHX file."""
        return cls(parse_fhx(read_fhx_text(path)), path)

    # ------------------------------------------------------------------ chunks

    def _build_chunks(self) -> Iterable[Chunk]:
        for top in self.nodes:
            scope = top.name or top.keyword
            header = FhxNode(top.keyword, top.attrs, top.props, [], top.line)
            yield Chunk(f"{scope}::{top.keyword}", scope, top.keyword, scope, render(header), top.line)
            children = [c for c in top.children if c.keyword not in NOISE_KEYWORDS]
            block_names = {c.name for c in children if c.name and c.keyword != "ATTRIBUTE_INSTANCE"}
            scoped: dict[str, list[FhxNode]] = defaultdict(list)
            for c in children:
                prefix = c.name.split("/", 1)[0] if "/" in c.name else ""
                if c.keyword == "ATTRIBUTE_INSTANCE" and prefix in block_names:
                    scoped[prefix].append(c)
            for c in children:
                if c.keyword == "ATTRIBUTE_INSTANCE" and c.name.split("/", 1)[0] in scoped and "/" in c.name:
                    continue
                text = render(c)
                if c.name in scoped:
                    text += "\n" + "\n".join(render(s) for s in scoped[c.name])
                yield from self._split(scope, c, text)

    def _split(self, scope: str, node: FhxNode, text: str) -> Iterable[Chunk]:
        base_id = f"{scope}::{node.keyword}:{node.name}" if node.name else f"{scope}::{node.keyword}@{node.line}"
        if len(text) <= MAX_CHUNK_CHARS or not node.children:
            yield Chunk(base_id, scope, node.keyword, node.name, text, node.line)
            return
        for sub in node.children:  # oversized (e.g. an SFC algorithm): index its parts
            if sub.keyword in NOISE_KEYWORDS:
                continue
            sub_id = f"{base_id}/{sub.keyword}:{sub.name}" if sub.name else f"{base_id}/{sub.keyword}@{sub.line}"
            yield Chunk(sub_id, scope, sub.keyword, sub.name, f"[in {node.keyword} {node.name}]\n{render(sub)}", sub.line)

    # -------------------------------------------------------------- references

    def references_of(self, node: FhxNode) -> list[str]:
        """Top-level block NAMEs that ``node`` (or its descendants) reference by value."""
        found: list[str] = []
        for n in node.walk():
            for v in list(n.attrs.values()) + list(n.props.values()):
                for s in v if isinstance(v, list) else [v]:
                    if isinstance(s, str) and s in self.top and s != node.name and s not in found:
                        found.append(s)
        return found

    def _reference_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = defaultdict(set)
        for top in self.nodes:
            for ref in self.references_of(top):
                graph[ref].add(top.name)
        return graph

    def root_blocks(self, keyword: str | None = None) -> list[str]:
        """Top-level blocks that no other block references (candidate main modules)."""
        return [n.name for n in self.nodes if n.name and not self.referenced_by.get(n.name)
                and (keyword is None or n.keyword == keyword)]

    # ---------------------------------------------------------------- selection

    def select(self, selector: Selector, max_nodes: int = 400) -> list[SelectedNode]:
        """Resolve a structural selector to rendered nodes.

        Path-scoped ATTRIBUTE_INSTANCE overrides of a selected block are appended to
        it, and referenced top-level blocks are appended when ``follow_references``.

        Raises:
            re.error: If a regex in the selector is invalid.
        """
        scopes = list(self.top.values()) if selector.scope == "*" else [self.top[selector.scope]] \
            if selector.scope in self.top else []
        name_rx = re.compile(selector.name_regex) if selector.name_regex else None
        text_rx = re.compile(selector.text_regex) if selector.text_regex else None
        out: list[SelectedNode] = []
        seen: set[str] = set()
        seen_nodes: set[int] = set()  # a definition reachable from several blocks is returned once
        for top in scopes:
            if selector.scope_is_target:
                header = FhxNode(top.keyword, top.attrs, top.props, [], top.line)
                out.append(SelectedNode(f"{top.name}::{top.keyword}", render(header)))
                if selector.keyword == "*" and not (name_rx or text_rx):
                    continue
            for path, child, parent in self._candidates(top, selector.deep):
                if selector.keyword != "*" and child.keyword != selector.keyword:
                    continue
                if name_rx and not name_rx.search(child.name):
                    continue
                text = render(child)
                if child.keyword != "ATTRIBUTE_INSTANCE" and child.name:
                    scoped = [c for c in parent.children if c.keyword == "ATTRIBUTE_INSTANCE"
                              and c.name.startswith(child.name + "/")]
                    if scoped:
                        text += "\n" + "\n".join(render(c) for c in scoped)
                if text_rx and not text_rx.search(text):
                    continue
                ref = f"{top.name}::{path}"
                if ref in seen or id(child) in seen_nodes:
                    continue
                seen.add(ref)
                seen_nodes.add(id(child))
                out.append(SelectedNode(ref, text))
                if selector.follow_references:
                    for target in self.references_of(child):
                        tref = f"{target}::{self.top[target].keyword}"
                        if tref not in seen:
                            seen.add(tref)
                            out.append(SelectedNode(tref, render(self.top[target])))
                if len(out) >= max_nodes:
                    if max_nodes > 1:
                        logger.warning("Selector %s truncated at %d nodes", selector, max_nodes)
                    return out
        return out

    def _candidates(self, top: FhxNode, deep: bool) -> Iterable[tuple[str, FhxNode, FhxNode]]:
        """(path, node, container) for direct children, or all descendants when ``deep``.

        Deep traversal also enters the top-level block a child references in its header
        (e.g. a block's definition), giving the same virtual paths DeltaV uses at run time
        (``MONITOR/HM01/DESC``). Control-module classes are not entered.
        """
        queue: list[tuple[str, FhxNode]] = [("", top)]
        visited = {top.name}
        while queue:
            prefix, container = queue.pop(0)
            for child in container.children:
                if child.keyword in NOISE_KEYWORDS:
                    continue
                label = f"{child.keyword}:{child.name}" if child.name else f"{child.keyword}@{child.line}"
                path = f"{prefix}/{label}" if prefix else label
                yield path, child, container
                if not deep:
                    continue
                if child.children:
                    queue.append((path, child))
                for value in child.attrs.values():
                    target = self.top.get(value) if isinstance(value, str) else None
                    if target is not None and value not in visited and target.keyword != "MODULE_CLASS":
                        visited.add(value)
                        queue.append((path, target))

    def search(self, query: str, top_k: int = 8, scope: str | None = None) -> list[Hit[Chunk]]:
        """Lexical search over chunks, optionally restricted to one top-level scope."""
        where = (lambda c: c.scope == scope) if scope else None
        return self.index.search(query, top_k, where)

    # ------------------------------------------------------------------ outline

    def outline(self, focus: str | None = None, max_names: int = 600) -> str:
        """Human/LLM-readable map of the file used by the cartographer.

        Args:
            focus: NAME of a top-level block to describe in full detail (all child names).
            max_names: Cap on listed child names per keyword group.
        """
        lines = ["# Top-level blocks (keyword NAME | description | referenced-by count | child keyword counts)"]
        for n in self.nodes:
            counts = Counter(c.keyword for c in n.children if c.keyword not in NOISE_KEYWORDS)
            desc = str(n.get("DESCRIPTION", ""))[:70]
            refs = len(self.referenced_by.get(n.name, ()))
            kids = ", ".join(f"{k}x{v}" for k, v in counts.most_common(8))
            lines.append(f"- {n.keyword} {n.name!r} | {desc} | refs={refs} | {kids}")
        if focus and focus in self.top:
            lines += self._children_outline(focus, max_names)
            # Embedded (block-owned) definitions the focus block uses, one level deep.
            for ref in self.references_of(self.top[focus]):
                if ref.startswith("__") and self.top[ref].keyword != "MODULE_CLASS":
                    lines += self._children_outline(ref, 80, header=f"embedded definition used by {focus}")
        return "\n".join(lines)

    def _children_outline(self, name: str, max_names: int, header: str = "") -> list[str]:
        node = self.top[name]
        desc = str(node.get("DESCRIPTION", ""))[:80]
        lines = [f"\n# Children of {node.keyword} {name!r}" + (f" ({header})" if header else "")
                 + (f" - {desc}" if desc else "")]
        groups: dict[str, list[str]] = defaultdict(list)
        for c in node.children:
            if c.keyword in NOISE_KEYWORDS:
                continue
            extra = c.attrs.get("DEFINITION") or c.attrs.get("MODULE") or c.attrs.get("TYPE") or ""
            groups[c.keyword].append(f"{c.name}({extra})" if extra else (c.name or "<unnamed>"))
        for kw, names in groups.items():
            shown = names[:max_names]
            more = f" ... (+{len(names) - len(shown)})" if len(names) > len(shown) else ""
            lines.append(f"- {kw} x{len(names)}: " + ", ".join(shown) + more)
        return lines

    # ---------------------------------------------------------------- grounding

    @property
    def flat_text(self) -> str:
        """All rendered content in one string, used by grounding guardrails."""
        if self._flat_text is None:
            self._flat_text = "\n".join(render(n) for n in self.nodes)
        return self._flat_text


class SourceBundle(BaseModel):
    """Rendered source handed to an agent, with citation refs."""

    refs: list[str] = Field(default_factory=list)
    text: str = ""

    @classmethod
    def from_nodes(cls, nodes: list[SelectedNode]) -> "SourceBundle":
        return cls(refs=[n.ref for n in nodes],
                   text="\n\n".join(f"### SOURCE [{n.ref}]\n{n.text}" for n in nodes))
