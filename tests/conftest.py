from __future__ import annotations

from pathlib import Path

import pytest

from dds_agent.fhx.source import FhxSource
from dds_agent.fhx.tree import parse_fhx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def mini_text() -> str:
    return (FIXTURES / "mini_em.fhx.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def mini_source(mini_text: str) -> FhxSource:
    return FhxSource(parse_fhx(mini_text), FIXTURES / "mini_em.fhx.txt")
