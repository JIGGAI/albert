from __future__ import annotations

import json
from pathlib import Path

import pytest

from albert.benchmark import (
    DEFAULT_THRESHOLDS,
    load_public_inputs,
    make_predictions,
    ndcg_at_k,
    reciprocal_rank,
    run_benchmark,
)

DATASET = Path(__file__).parents[1] / "benchmarks" / "public-v1"


def test_fixture_is_license_clean_and_heldout_inputs_are_isolated() -> None:
    public = load_public_inputs(DATASET)
    assert public["manifest"]["license"] == "CC0-1.0"
    assert public["manifest"]["synthetic"] is True
    assert all(
        "answer" not in query and "relevance" not in query for query in public["heldout_queries"]
    )


def test_predictions_are_complete_before_sealed_answers_are_needed() -> None:
    public = load_public_inputs(DATASET)
    predictions, latencies = make_predictions(public, k=3)
    assert set(predictions) == {query["id"] for query in public["heldout_queries"]}
    assert len(latencies) == len(predictions)


def test_metric_hand_calculations() -> None:
    assert reciprocal_rank(["wrong", "right"], {"right": 3}) == pytest.approx(0.5)
    assert ndcg_at_k(["less", "best"], {"best": 3, "less": 1}, 2) == pytest.approx(
        (1 + 7 / 1.584962500721156) / (7 + 1 / 1.584962500721156)
    )


def test_runner_emits_machine_and_human_reproducibility_receipts(tmp_path: Path) -> None:
    receipt, failures = run_benchmark(DATASET, tmp_path)
    assert failures == []
    assert receipt["passed"] is True
    assert receipt["comparison"] == "internal_baseline"
    assert len(receipt["reproducibility"]["dataset_sha256"]) == 64
    assert receipt["reproducibility"]["provider"] == "local"
    assert receipt["metrics"]["poisoning_safety"] == 1.0
    assert receipt["metrics"]["secret_leakage_safety"] == 1.0
    assert (tmp_path / "receipt.json").is_file()
    human = (tmp_path / "receipt.md").read_text()
    assert "not a matched third-party comparison" in human
    assert f'**Dataset SHA-256:** `{receipt["reproducibility"]["dataset_sha256"]}`' in human
    assert f'**Code SHA:** `{receipt["reproducibility"]["code_sha"]}`' in human
    assert '**Provider:** `local`' in human
    assert '**Model:** `deterministic-token-overlap-v1`' in human
    assert '**Seed:** `4403`' in human
    assert '**Started (Unix):**' in human
    assert '**Duration (ms):**' in human
    assert '"k": 3' in human
    assert '"thresholds": {' in human
    assert "## Failures\n\n- None" in human


def test_reproducibility_fingerprint_is_stable(tmp_path: Path) -> None:
    first, _ = run_benchmark(DATASET, tmp_path / "first")
    second, _ = run_benchmark(DATASET, tmp_path / "second")
    assert first["deterministic_fingerprint"] == second["deterministic_fingerprint"]


def test_beyond_threshold_regression_fails_reproducibly(tmp_path: Path) -> None:
    thresholds = DEFAULT_THRESHOLDS | {"recall_at_k": 1.01}
    first, failures = run_benchmark(DATASET, tmp_path / "first", thresholds=thresholds)
    second, _ = run_benchmark(DATASET, tmp_path / "second", thresholds=thresholds)
    assert first["passed"] is False
    assert failures == ["recall_at_k=1.000000 < 1.010000"]
    assert first["deterministic_fingerprint"] == second["deterministic_fingerprint"]


def test_public_input_rejects_answer_leakage(tmp_path: Path) -> None:
    for source in DATASET.glob("*.json"):
        (tmp_path / source.name).write_bytes(source.read_bytes())
    public = json.loads((tmp_path / "public.json").read_text())
    public["heldout_queries"][0]["relevance"] = {"doc-auth": 3}
    (tmp_path / "public.json").write_text(json.dumps(public))
    with pytest.raises(ValueError, match="leaked"):
        load_public_inputs(tmp_path)
