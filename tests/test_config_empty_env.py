"""compose.yaml passes `ALBERT_GRAPH_BUILDER: ${ALBERT_GRAPH_BUILDER:-}` and
.env.example ships `ALBERT_GRAPH_BUILDER=`, so an operator who leaves the
variable blank hands Settings an empty string. That must read as "not set"
(pick the builder from the LLM provider), not fail validation — which took a
deployment down on the migrate step."""

from __future__ import annotations

import pytest

from albert.config import Settings


@pytest.mark.parametrize(
    ("llm_provider", "expected"),
    [("none", "regex"), ("openai-compatible", "llm-classifier")],
)
def test_blank_graph_builder_falls_back_to_provider_default(
    monkeypatch: pytest.MonkeyPatch, llm_provider: str, expected: str
) -> None:
    monkeypatch.setenv("ALBERT_GRAPH_BUILDER", "")
    monkeypatch.setenv("ALBERT_LLM_PROVIDER", llm_provider)
    if llm_provider == "openai-compatible":
        monkeypatch.setenv("ALBERT_OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("ALBERT_CLASSIFICATION_MODEL", "gpt-test")

    settings = Settings(_env_file=None)

    assert settings.graph_builder == expected


def test_whitespace_graph_builder_is_also_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALBERT_GRAPH_BUILDER", "   ")
    monkeypatch.setenv("ALBERT_LLM_PROVIDER", "none")

    assert Settings(_env_file=None).graph_builder == "regex"
