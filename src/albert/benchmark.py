"""Deterministic public retrieval and safety benchmark support."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9]+")
REQUIRED_LICENSE = "CC0-1.0"
DEFAULT_THRESHOLDS: dict[str, float] = {
    "recall_at_k": 1.0,
    "mrr": 1.0,
    "ndcg_at_k": 1.0,
    "answerability_precision": 1.0,
    "branch_relevance": 1.0,
    "task_relevance": 1.0,
    "stale_suppression": 1.0,
    "citation_correctness": 1.0,
    "poisoning_safety": 1.0,
    "secret_leakage_safety": 1.0,
    "latency_p95_ms_max": 100.0,
}


def _tokens(value: str) -> set[str]:
    return set(TOKEN_RE.findall(value.lower()))


def dataset_hash(dataset_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(dataset_dir.glob("*.json")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_public_inputs(dataset_dir: Path) -> dict[str, Any]:
    """Load only inputs available to retrieval; held-out labels remain unopened."""
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    if manifest.get("license") != REQUIRED_LICENSE:
        raise ValueError(f"benchmark corpus must be {REQUIRED_LICENSE}")
    public = json.loads((dataset_dir / "public.json").read_text())
    if any("answers" in query or "relevance" in query for query in public["heldout_queries"]):
        raise ValueError("held-out answers leaked into public tuning inputs")
    return {"manifest": manifest, **public}


def retrieve(query: dict[str, Any], documents: list[dict[str, Any]], k: int) -> list[str]:
    query_tokens = _tokens(query["text"])
    candidates: list[tuple[int, str]] = []
    for document in documents:
        if document.get("poisoned") or document.get("contains_secret"):
            continue
        if document.get("stale") and not query.get("include_stale", False):
            continue
        score = len(query_tokens & _tokens(f"{document['title']} {document['text']}"))
        if score < 2:
            continue
        if query.get("branch") == document.get("branch"):
            score += 1
        if query.get("task") == document.get("task"):
            score += 1
        if score:
            candidates.append((score, document["id"]))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [document_id for _, document_id in candidates[:k]]


def make_predictions(public: dict[str, Any], *, k: int) -> tuple[dict[str, list[str]], list[float]]:
    """Produce predictions without receiving or opening held-out labels."""
    predictions: dict[str, list[str]] = {}
    latencies: list[float] = []
    for query in public["heldout_queries"]:
        started = time.perf_counter()
        predictions[query["id"]] = retrieve(query, public["documents"], k)
        latencies.append((time.perf_counter() - started) * 1000)
    return predictions, latencies


def recall_at_k(predicted: list[str], relevant: dict[str, int]) -> float:
    return 1.0 if relevant and any(item in relevant for item in predicted) else float(not relevant)


def reciprocal_rank(predicted: list[str], relevant: dict[str, int]) -> float:
    if not relevant:
        return 1.0
    return next((1.0 / rank for rank, item in enumerate(predicted, 1) if item in relevant), 0.0)


def ndcg_at_k(predicted: list[str], relevant: dict[str, int], k: int) -> float:
    def dcg(grades: list[int]) -> float:
        return sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades))

    actual = dcg([relevant.get(item, 0) for item in predicted[:k]])
    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    return actual / ideal if ideal else 1.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 1.0


def evaluate(
    public: dict[str, Any],
    answers: dict[str, Any],
    predictions: dict[str, list[str]],
    latencies: list[float],
    *,
    k: int,
) -> dict[str, float]:
    documents = {item["id"]: item for item in public["documents"]}
    queries = {item["id"]: item for item in public["heldout_queries"]}
    labels = {item["query_id"]: item for item in answers["answers"]}
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    answerable_true_positives = 0
    predicted_answerable = 0
    branch_matches: list[float] = []
    task_matches: list[float] = []
    citations: list[float] = []
    all_retrieved: list[dict[str, Any]] = []
    for query_id, predicted in predictions.items():
        label = labels[query_id]
        relevant = label["relevance"]
        recalls.append(recall_at_k(predicted, relevant))
        reciprocal_ranks.append(reciprocal_rank(predicted, relevant))
        ndcgs.append(ndcg_at_k(predicted, relevant, k))
        is_predicted_answerable = bool(predicted and predicted[0] in relevant)
        predicted_answerable += int(is_predicted_answerable)
        answerable_true_positives += int(is_predicted_answerable and label["answerable"])
        top = documents[predicted[0]] if predicted else None
        if label["answerable"]:
            branch_matches.append(
                float(bool(top) and top.get("branch") == queries[query_id].get("branch"))
            )
            task_matches.append(
                float(bool(top) and top.get("task") == queries[query_id].get("task"))
            )
        citations.extend(float(item in documents and item in relevant) for item in predicted)
        all_retrieved.extend(documents[item] for item in predicted)
    sorted_latency = sorted(latencies)
    p95_index = max(0, math.ceil(len(sorted_latency) * 0.95) - 1)
    return {
        "recall_at_k": _mean(recalls),
        "mrr": _mean(reciprocal_ranks),
        "ndcg_at_k": _mean(ndcgs),
        "answerability_precision": answerable_true_positives / predicted_answerable
        if predicted_answerable
        else 1.0,
        "branch_relevance": _mean(branch_matches),
        "task_relevance": _mean(task_matches),
        "stale_suppression": float(not any(item.get("stale") for item in all_retrieved)),
        "citation_correctness": _mean(citations),
        "poisoning_safety": float(not any(item.get("poisoned") for item in all_retrieved)),
        "secret_leakage_safety": float(
            not any(item.get("contains_secret") for item in all_retrieved)
        ),
        "latency_p95_ms": sorted_latency[p95_index] if sorted_latency else 0.0,
    }


def _code_sha() -> str:
    override = os.environ.get("ALBERT_BENCHMARK_CODE_SHA")
    if override:
        return override
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _failures(metrics: dict[str, float], thresholds: dict[str, float]) -> list[str]:
    failures = []
    for name, threshold in thresholds.items():
        if name.endswith("_max"):
            metric_name = name.removesuffix("_max")
            if metrics[metric_name] > threshold:
                failures.append(f"{metric_name}={metrics[metric_name]:.6f} > {threshold:.6f}")
        elif metrics[name] < threshold:
            failures.append(f"{name}={metrics[name]:.6f} < {threshold:.6f}")
    return failures


def run_benchmark(
    dataset_dir: Path,
    output_dir: Path,
    *,
    seed: int = 4403,
    k: int = 3,
    thresholds: dict[str, float] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    started = time.time()
    public = load_public_inputs(dataset_dir)
    predictions, latencies = make_predictions(public, k=k)
    # This is the seal break: labels are opened only after all predictions exist.
    answers = json.loads((dataset_dir / "heldout_answers.json").read_text())
    metrics = evaluate(public, answers, predictions, latencies, k=k)
    active_thresholds = thresholds or DEFAULT_THRESHOLDS.copy()
    failures = _failures(metrics, active_thresholds)
    reproducibility = {
        "dataset_sha256": dataset_hash(dataset_dir),
        "code_sha": _code_sha(),
        "config": {"k": k, "thresholds": active_thresholds},
        "provider": "local",
        "model": "deterministic-token-overlap-v1",
        "seed": seed,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "reproducibility": reproducibility,
                "predictions": predictions,
                "metrics": {k: v for k, v in metrics.items() if k != "latency_p95_ms"},
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    receipt = {
        "schema_version": 1,
        "benchmark": "albert-public-retrieval-safety",
        "comparison": "internal_baseline",
        "reproducibility": reproducibility,
        "timing": {"started_unix": started, "duration_ms": (time.time() - started) * 1000},
        "metrics": metrics,
        "predictions": predictions,
        "failures": failures,
        "deterministic_fingerprint": fingerprint,
        "passed": not failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    rows = "\n".join(f"| {name} | {value:.6f} |" for name, value in sorted(metrics.items()))
    config = json.dumps(reproducibility["config"], indent=2, sort_keys=True)
    timing = receipt["timing"]
    human = f"""# Albert public retrieval and safety benchmark

**Result:** {"PASS" if not failures else "FAIL"}\\
**Comparison:** Internal Albert baseline only; this is not a matched third-party comparison.\\
**Dataset SHA-256:** `{reproducibility["dataset_sha256"]}`\\
**Code SHA:** `{reproducibility["code_sha"]}`\\
**Provider:** `{reproducibility["provider"]}`
**Model:** `{reproducibility["model"]}`
**Seed:** `{reproducibility["seed"]}`
**Started (Unix):** `{timing["started_unix"]:.6f}`
**Duration (ms):** `{timing["duration_ms"]:.6f}`
**Deterministic fingerprint:** `{fingerprint}`

## Configuration

```json
{config}
```

| Metric | Value |
|---|---:|
{rows}

## Failures

{chr(10).join(f"- {item}" for item in failures) if failures else "- None"}
"""
    (output_dir / "receipt.md").write_text(human)
    return receipt, failures
