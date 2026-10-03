from __future__ import annotations

import contextlib
import statistics
import time

import httpx
import pytest

from albert import tracing
from albert.trace_writer import get_trace_writer

pytestmark = pytest.mark.anyio


async def _median_ms(client: httpx.AsyncClient, rounds: int) -> float:
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        response = await client.post(
            "/v1/search", json={"query": "overhead probe", "include_graph": False}
        )
        assert response.status_code == 200
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


async def test_recording_overhead_is_within_budget(
    client: httpx.AsyncClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    await _median_ms(client, 5)
    with_recording = await _median_ms(client, 40)
    get_trace_writer().flush()
    # With activate() neutralised no recorder is current, so every span is a
    # no-op and nothing is submitted; the middleware itself still runs.
    monkeypatch.setattr(tracing, "activate", lambda r: contextlib.nullcontext(r))
    without = await _median_ms(client, 40)
    overhead = with_recording - without
    assert overhead < max(2.0, without * 0.05), f"{with_recording=:.2f} {without=:.2f}"
