from __future__ import annotations

import argparse
import json
from pathlib import Path

from albert.benchmark import DEFAULT_THRESHOLDS, run_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Albert's public retrieval/safety benchmark")
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/public-v1"))
    parser.add_argument("--output", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--seed", type=int, default=4403)
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args()
    thresholds = DEFAULT_THRESHOLDS.copy()
    if args.thresholds:
        thresholds.update(json.loads(args.thresholds.read_text()))
    receipt, failures = run_benchmark(
        args.dataset, args.output, seed=args.seed, k=args.k, thresholds=thresholds
    )
    print(
        json.dumps(
            {
                "passed": receipt["passed"],
                "failures": failures,
                "receipt": str(args.output / "receipt.json"),
            }
        )
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
