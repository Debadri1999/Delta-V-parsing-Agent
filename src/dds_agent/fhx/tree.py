"""Syntax-only FHX reader: decoding, tokenizer and a generic block tree.

FHX exports are text made of nested blocks::

    KEYWORD ATTR="value" ATTR=value
    {
      PROPERTY="value"
      PROPERTY= { X=1 Y=2 }
      CHILD_KEYWORD NAME="..." { ... }
    }

This module knows the *syntax* only (strings with ``""`` escapes, split string
literals, ``/* */`` comments, nested blocks). It assigns no DeltaV meaning to
any keyword; interpreting the content is the agents' job.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>/\*.*?\*/)
    | (?P<string>"(?:[^"]|"")*")
    | (?P<punct>[{}=])
    | (?P<word>(?:[^\s={}"/]|/(?!\*))+)
    """,
    re.S | re.X,
)


def read_fhx_text(path: Path) -> str:
    """Decode an FHX file (UTF-16 is DeltaV's default; UTF-8 and cp1252 also accepted).

    Args:
        path: File to read.

    Returns:
        Text with normalized ``\\n`` line endings.
    """
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    elif len(raw) > 1 and raw[1:2] == b"\x00":
        text = raw.decode("utf-16-le")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Token:
    """Lexical token. ``kind`` is ``S`` (string), ``W`` (word), ``{``, ``}`` or ``=``."""

    kind: str
    value: str
    line: int


def tokenize(text: str) -> list[Token]:
    """Split FHX text into tokens, dropping whitespace and comments."""
    tokens: list[Token] = []
    line = 1
    for m in _TOKEN_RE.finditer(text):
        kind, tok = m.lastgroup, m.group(0)
        if kind == "string":
            tokens.append(Token("S", tok[1:-1].replace('""', '"'), line))
        elif kind == "punct":
            tokens.append(Token(tok, tok, line))
        elif kind == "word":
            tokens.append(Token("W", tok, line))
        line += tok.count("\n")
    return tokens


@dataclass
class FhxNode:
    """One keyword block.

    Attributes:
        keyword: Block keyword, e.g. ``MODULE_CLASS``.
        attrs: Header attributes written before ``{`` (``NAME``, ``TYPE``...).
        props: ``KEY=value`` body properties; a ``dict`` for inline ``{ }`` structs,
            a ``list`` when a key repeats.
        children: Nested keyword blocks.
        line: Source line of the keyword (1-based).
    """

    keyword: str
    attrs: dict[str, Any] = field(default_factory=dict)
    props: dict[str, Any] = field(default_factory=dict)
    children: list["FhxNode"] = field(default_factory=list)
    line: int = 0

    @property
    def name(self) -> str:
        """``NAME`` attribute, or ``""``."""
        return str(self.attrs.get("NAME", self.props.get("NAME", "")))

    def get(self, key: str, default: Any = "") -> Any:
        """Property value, falling back to header attributes."""
        if key in self.props:
            return self.props[key]
        return self.attrs.get(key, default)

    def walk(self) -> Iterator["FhxNode"]:
        """Yield this node and all descendants depth-first."""
        yield self
        for child in self.children:
            yield from child.walk()


class _TreeBuilder:
    """Recursive-descent parser over tokens. Tolerates truncated input."""

    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.i = 0
        self.unclosed_blocks = 0

    def _peek(self, offset: int = 0) -> Token | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def _value(self) -> Any:
        tok = self._peek()
        if tok is None:
            return ""
        self.i += 1
        if tok.kind == "{":
            struct: dict[str, Any] = {}
            while (nxt := self._peek()) is not None and nxt.kind != "}":
                self.i += 1
                if (eq := self._peek()) is not None and eq.kind == "=":
                    self.i += 1
                    struct[nxt.value] = self._value()
                else:
                    struct.setdefault("_flags", []).append(nxt.value)
            if self._peek() is not None:
                self.i += 1
            return struct
        if tok.kind == "S":
            value = tok.value
            while (nxt := self._peek()) is not None and nxt.kind == "S":  # split literal
                value += nxt.value
                self.i += 1
            return value
        return tok.value

    def parse_body(self, closing: bool) -> tuple[dict[str, Any], list[FhxNode]]:
        props: dict[str, Any] = {}
        repeated: set[str] = set()
        children: list[FhxNode] = []
        while (tok := self._peek()) is not None:
            if tok.kind == "}":
                self.i += 1
                if closing:
                    return props, children
                logger.warning("Unbalanced '}' at line %d ignored", tok.line)
                continue
            if tok.kind != "W":
                self.i += 1
                continue
            self.i += 1
            nxt = self._peek()
            if nxt is not None and nxt.kind == "=":
                self.i += 1
                value = self._value()
                if tok.value in props:
                    if tok.value not in repeated:
                        props[tok.value] = [props[tok.value]]
                        repeated.add(tok.value)
                    props[tok.value].append(value)
                else:
                    props[tok.value] = value
                continue
            node = FhxNode(keyword=tok.value, line=tok.line)
            while (h := self._peek()) is not None:
                after = self._peek(1)
                if h.kind == "W" and after is not None and after.kind == "=":
                    self.i += 2
                    node.attrs[h.value] = self._value()
                elif h.kind == "{":
                    self.i += 1
                    node.props, node.children = self.parse_body(closing=True)
                    break
                else:
                    break
            children.append(node)
        if closing:
            self.unclosed_blocks += 1
        return props, children


def parse_fhx(text: str) -> list[FhxNode]:
    """Parse FHX text into top-level blocks.

    Args:
        text: Decoded FHX text.

    Returns:
        Top-level nodes in file order.
    """
    builder = _TreeBuilder(tokenize(text))
    _, nodes = builder.parse_body(closing=False)
    if builder.unclosed_blocks:
        logger.warning("File ended inside %d open block(s); the export may be truncated", builder.unclosed_blocks)
    return nodes
