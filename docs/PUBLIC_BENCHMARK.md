# Public retrieval and safety benchmark

Albert publishes a synthetic, CC0-licensed corpus in `benchmarks/public-v1`.
The public retrieval inputs contain no held-out answers. The runner finalizes all
predictions before opening `heldout_answers.json`, preventing labels from entering
the evaluated retrieval path. The fixtures contain no real credentials or customer
data; the secret-shaped value is explicitly synthetic.

Run from a clean checkout:

```sh
python scripts/run-public-benchmark.py --output benchmark-results
```

The command writes machine-readable `receipt.json` and human-readable `receipt.md`.
Both record the dataset hash, code SHA, configuration, provider/model, seed, timing,
failures, and a deterministic fingerprint. CI applies the declared retrieval and
safety thresholds and fails on regression.

Reported numbers are an **internal Albert baseline**. They are not a matched
third-party comparison. Any future third-party claim must use the same corpus,
configuration, hardware class, provider/model, and evaluation procedure and must
name those conditions in its receipt.
