"""Runtime settings, read from environment variables (and ``.env``).

FHX exports are AstraZeneca-confidential. :func:`assert_on_prem` blocks any LLM
endpoint other than Purdue GenAI Studio, and every LLM call goes through it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR: Path = Path(__file__).resolve().parents[2]
PACKAGE_DIR: Path = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

ALLOWED_API_HOSTS: tuple[str, ...] = ("genai.rcac.purdue.edu",)


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    """All tunables in one place. Override any field with the matching ``DDS_*`` env var.

    Attributes:
        base_url: OpenAI-compatible endpoint (Purdue GenAI Studio).
        generator_model: Model used by the cartographer and parser agents.
        critic_model: Model used by the critic agent. A different model family
            than the generator gives a more independent review.
        max_prompt_tokens: Budget for one prompt, leaving room for the answer within the
            served models' 65,536-token context.
        max_output_tokens: Completion cap per call.
        max_iterations: Parse -> critique -> revise rounds per section.
        pass_score: Critic score (0-10) at which a section stops revising.
        release_min_mean: Mean section score needed for release.
        release_min_section: Lowest section score allowed for release.
        exemplar_dir: Folder of hand-written DDS ``.docx`` files used as RAG exemplars.
        output_dir: Where run artifacts and PDFs are written.
    """

    base_url: str = field(default_factory=lambda: _env("DDS_BASE_URL", "https://genai.rcac.purdue.edu/api"))
    generator_model: str = field(default_factory=lambda: _env("DDS_GENERATOR_MODEL", "gpt-oss:120b"))
    critic_model: str = field(default_factory=lambda: _env("DDS_CRITIC_MODEL", "gemma4:26b-a4b"))
    max_prompt_tokens: int = field(default_factory=lambda: int(_env("DDS_MAX_PROMPT_TOKENS", "40000")))
    max_output_tokens: int = field(default_factory=lambda: int(_env("DDS_MAX_OUTPUT_TOKENS", "16000")))
    request_timeout_s: float = field(default_factory=lambda: float(_env("DDS_REQUEST_TIMEOUT_S", "600")))
    temperature: float = field(default_factory=lambda: float(_env("DDS_TEMPERATURE", "0")))
    max_iterations: int = field(default_factory=lambda: int(_env("DDS_MAX_ITERATIONS", "3")))
    pass_score: float = field(default_factory=lambda: float(_env("DDS_PASS_SCORE", "8.5")))
    release_min_mean: float = field(default_factory=lambda: float(_env("DDS_RELEASE_MIN_MEAN", "8.0")))
    release_min_section: float = field(default_factory=lambda: float(_env("DDS_RELEASE_MIN_SECTION", "6.0")))
    exemplar_dir: Path = field(default_factory=lambda: Path(_env("DDS_EXEMPLAR_DIR", str(PROJECT_DIR / "data" / "exemplars"))))
    output_dir: Path = field(default_factory=lambda: Path(_env("DDS_OUTPUT_DIR", str(PROJECT_DIR / "output"))))
    rate_limit_backoff_s: tuple[float, ...] = (5, 10, 20, 30, 60, 60)

    @property
    def api_key(self) -> str:
        """GenAI Studio API key from ``GENAI_API_KEY``.

        Raises:
            RuntimeError: If the key is missing.
        """
        key = os.getenv("GENAI_API_KEY", "").strip()
        if not key or key.startswith("<"):
            raise RuntimeError(f"GENAI_API_KEY is not set. Add it to {PROJECT_DIR / '.env'}.")
        return key


def assert_on_prem(url: str) -> None:
    """Refuse to send data anywhere except Purdue GenAI Studio.

    Args:
        url: Base URL about to receive a request.

    Raises:
        RuntimeError: If the host is not allow-listed.
    """
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    if host not in ALLOWED_API_HOSTS:
        raise RuntimeError(f"Refusing to send NDA data to non-Purdue host: {host}")
