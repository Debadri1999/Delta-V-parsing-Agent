"""Local lexical retriever (TF-IDF) shared by the FHX source index and the knowledge base.

Lexical retrieval is the right tool here: FHX content is dominated by exact
identifiers (``EM_MIX_SPD_SP``, ``COMMAND_00002``, ``HM02/DESC``) that dense
embeddings blur, and it runs fully offline, so no NDA text leaves the machine
for indexing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

T = TypeVar("T")

_WORD_RE = re.compile(r"[A-Za-z0-9_$]+")


def identifier_analyzer(text: str) -> list[str]:
    """Tokenize text, keeping whole identifiers plus their ``_``/``/`` parts.

    ``'^/AGITATOR/REQ_SP.CV'`` yields ``agitator``, ``req_sp``, ``req``, ``sp``, ``cv``.
    """
    terms: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        terms.append(word)
        if "_" in word:
            terms.extend(p for p in word.split("_") if len(p) > 1)
    return terms


@dataclass(frozen=True)
class Hit(Generic[T]):
    """A retrieval result."""

    item: T
    score: float


class TfidfIndex(Generic[T]):
    """TF-IDF index over arbitrary items.

    Args:
        items: Items to index.
        text_of: Returns the searchable text of an item.
    """

    def __init__(self, items: Sequence[T], text_of: Callable[[T], str]) -> None:
        self.items = list(items)
        self._vectorizer = TfidfVectorizer(analyzer=identifier_analyzer, sublinear_tf=True)
        corpus = [text_of(i) for i in self.items]
        self._matrix = None
        if any(identifier_analyzer(t) for t in corpus):
            self._matrix = self._vectorizer.fit_transform(corpus)

    def search(self, query: str, top_k: int = 8, where: Callable[[T], bool] | None = None) -> list[Hit[T]]:
        """Return the ``top_k`` best matches with a positive score.

        Args:
            query: Free text or identifiers.
            top_k: Maximum number of hits.
            where: Optional filter applied before ranking.
        """
        if self._matrix is None or not query.strip():
            return []
        scores = linear_kernel(self._vectorizer.transform([query]), self._matrix).ravel()
        ranked = sorted(range(len(self.items)), key=lambda i: scores[i], reverse=True)
        hits: list[Hit[T]] = []
        for i in ranked:
            if scores[i] <= 0 or len(hits) >= top_k:
                break
            if where is None or where(self.items[i]):
                hits.append(Hit(self.items[i], float(scores[i])))
        return hits
