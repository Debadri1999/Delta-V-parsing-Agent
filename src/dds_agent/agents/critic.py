"""Critic agent: independent LLM review of a drafted section against source and rules.

Runs on a different model family than the parser so the review does not share the
parser's blind spots. Its findings, together with the deterministic guardrails,
drive the revision loop and the release gate.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError, field_validator

from dds_agent.agents import prompts
from dds_agent.config import Settings
from dds_agent.fhx.source import SourceBundle
from dds_agent.guardrails.checks import GuardrailReport
from dds_agent.knowledge.store import KnowledgeStore
from dds_agent.llm.client import LLMClient, LLMResponseError, PromptTooLargeError
from dds_agent.schema.dds import SectionSpec

logger = logging.getLogger(__name__)

# The critic model's tokenizer is denser than the parser's; keep its source shorter.
CRITIC_SOURCE_CHARS = 60_000


class Issue(BaseModel):
    severity: str = "minor"
    row: str = ""
    field: str = ""
    problem: str = ""
    fix: str = ""

    @field_validator("row", "field", "problem", "fix", "severity", mode="before")
    @classmethod
    def _str(cls, v: object) -> str:
        return "" if v is None else str(v)

    def render(self) -> str:
        where = "/".join(x for x in (self.row, self.field) if x)
        return f"[{self.severity}] {where + ': ' if where else ''}{self.problem}" + (f" -> {self.fix}" if self.fix else "")


class Review(BaseModel):
    """Critic verdict for one section draft."""

    completeness: float = 0
    faithfulness: float = 0
    conformance: float = 0
    score: float = 0
    issues: list[Issue] = Field(default_factory=list)
    summary: str = ""
    failed: bool = False


class CriticAgent:
    """Scores section drafts."""

    def __init__(self, llm: LLMClient, kb: KnowledgeStore, settings: Settings) -> None:
        self.llm = llm
        self.kb = kb
        self.settings = settings

    def review(self, spec: SectionSpec, module: str, draft: dict, source: SourceBundle,
               guardrails: GuardrailReport, label: str | None = None) -> Review:
        """Review a draft. A failed review scores 0 so it can never pass the gate silently."""
        label = label or spec.id
        src = source.text
        if len(src) > CRITIC_SOURCE_CHARS:
            src = src[:CRITIC_SOURCE_CHARS] + "\n... [source truncated for review]"
        user = prompts.CRITIC_USER.format(
            title=spec.title.format(module=module), section_id=spec.id, module=module,
            rules=self.kb.rules_for(spec.id, include_general=False),
            guardrails=guardrails.render() or "none", draft=json.dumps(draft, indent=1)[:30000], source=src)
        try:
            raw = self.llm.chat_json(agent=f"critic:{label}", model=self.settings.critic_model,
                                     system=prompts.CRITIC_SYSTEM, user=user, max_tokens=6000)
            review = Review.model_validate(raw)
        except (LLMResponseError, PromptTooLargeError, ValidationError) as exc:
            logger.error("critic:%s failed: %s", label, exc)
            return Review(failed=True, summary=f"review failed: {exc}")
        dims = [review.completeness, review.faithfulness, review.conformance]
        review.score = round(min([review.score or 10, *dims]) if any(dims) else review.score, 2)
        return review
