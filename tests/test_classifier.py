from albert.classifier import contains_likely_secret, heuristic_classify
from albert.embeddings import HashingEmbedder


def test_secret_detector() -> None:
    assert contains_likely_secret("password: super-secret-value")
    assert not contains_likely_secret("password policy requires fourteen characters")


def test_heuristic_relationship_extraction() -> None:
    result = heuristic_classify("Albert uses PostgreSQL. Albert depends on pgvector.")
    assert {relationship.relation_type for relationship in result.relationships} == {
        "uses",
        "depends_on",
    }


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbedder(64)
    first = embedder.embed("the same sentence")
    second = embedder.embed("the same sentence")
    assert first == second
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6
