from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from albert import classifier, embeddings
from albert.classifier import classify, contains_likely_secret, heuristic_classify
from albert.config import Settings
from albert.embeddings import HashingEmbedder, OpenAICompatibleEmbedder


def test_secret_detector() -> None:
    assert contains_likely_secret("password: super-secret-value")
    assert not contains_likely_secret("password policy requires fourteen characters")


def test_heuristic_relationship_extraction() -> None:
    result = heuristic_classify(
        "Albert uses PostgreSQL. Albert depends on pgvector. Albert runs on Linux."
    )
    assert {relationship.relation_type for relationship in result.relationships} == {
        "uses",
        "depends_on",
        "runs_on",
    }


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbedder(64)
    first = embedder.embed("the same sentence")
    second = embedder.embed("the same sentence")
    assert first == second
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_openai_embedding_provider_normalizes_and_validates_dimensions(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        embeddings,
        "get_settings",
        lambda: SimpleNamespace(
            openai_base_url="https://provider.example/v1",
            openai_api_key=SecretStr("test-secret"),
            embedding_request_dimensions=False,
        ),
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"embedding": [3.0, 4.0]}]}

    monkeypatch.setattr(embeddings.httpx, "post", lambda *args, **kwargs: Response())
    vector = OpenAICompatibleEmbedder("example", 2).embed("hello")
    assert vector == [0.6, 0.8]


def test_provider_configuration_requires_credentials() -> None:
    common = {
        "_env_file": None,
        "api_key_pepper": "test-pepper-that-is-longer-than-thirty-two-characters",
    }
    with pytest.raises(ValidationError, match="ALBERT_OPENAI_API_KEY"):
        Settings(**common, embedding_provider="openai-compatible", openai_api_key=None)

    with pytest.raises(ValidationError, match="ALBERT_CLASSIFICATION_MODEL"):
        Settings(
            **common,
            llm_provider="openai-compatible",
            openai_api_key="test-secret",
            classification_model=None,
        )


def test_model_classifier_parses_typed_graph_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        classifier,
        "get_settings",
        lambda: SimpleNamespace(
            llm_provider="openai-compatible",
            openai_base_url="https://provider.example/v1",
            openai_api_key=SecretStr("test-secret"),
            classification_model="classifier-model",
        ),
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"memory_type":"decision","sensitivity":"internal",'
                                '"confidence":0.9,"entities":['
                                '{"name":"Albert","entity_type":"project"},'
                                '{"name":"PostgreSQL","entity_type":"database"}],'
                                '"relationships":[{"source":"Albert",'
                                '"relation_type":"uses","target":"PostgreSQL",'
                                '"confidence":0.95}]}'
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(classifier.httpx, "post", lambda *args, **kwargs: Response())
    result = classify("Albert uses PostgreSQL.")
    assert result.memory_type == "decision"
    assert result.relationships[0].relation_type == "uses"
    assert result.relationships[0].target.entity_type == "database"


def _model_settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_provider="openai-compatible",
        openai_base_url="https://provider.example/v1",
        openai_api_key=SecretStr("test-secret"),
        classification_model="classifier-model",
    )


def test_model_prompt_lists_the_allowed_types_and_sensitivities(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(classifier, "get_settings", _model_settings)
    sent: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"memory_type":"fact"}'}}]}

    def post(_url: str, **kwargs):  # type: ignore[no-untyped-def]
        sent.update(kwargs["json"])
        return Response()

    monkeypatch.setattr(classifier.httpx, "post", post)
    classifier.classify("Albert uses PostgreSQL.")
    system = sent["messages"][0]["content"]
    for allowed in (*classifier.MEMORY_TYPES, *classifier.SENSITIVITIES):
        assert allowed in system


def test_model_output_outside_the_vocabulary_is_coerced_not_fatal() -> None:
    result = classifier._parse_model_classification(
        {
            "memory_type": "Policy",
            "sensitivity": "secret-ish",
            "confidence": "high",
            "entities": [
                {"name": "Lead Triage", "entity_type": "process", "confidence": None},
                "not-an-object",
                {"name": ""},
            ],
            "relationships": [
                {"source": "Lead Triage", "relation_type": "Runs Every", "target": "Nobody"},
                None,
            ],
        }
    )
    # An unusable label must not fail indexing: the memory keeps a safe default.
    assert result.memory_type == "fact"
    assert result.sensitivity == "internal"
    assert result.confidence == 0.7
    assert [entity.name for entity in result.entities] == ["Lead Triage"]
    assert result.relationships == []  # an edge to an unknown entity is dropped


def test_model_output_is_bounded() -> None:
    entities = [{"name": f"Entity {i}", "entity_type": "thing"} for i in range(200)]
    relationships = [
        {"source": "Entity 0", "relation_type": "links", "target": f"Entity {i}"}
        for i in range(1, 200)
    ]
    result = classifier._parse_model_classification(
        {"memory_type": "fact", "entities": entities, "relationships": relationships}
    )
    assert len(result.entities) == classifier.MAX_ENTITIES
    assert len(result.relationships) <= classifier.MAX_RELATIONSHIPS
