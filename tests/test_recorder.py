from __future__ import annotations

import json
import time

import pytest

from albert import recorder as rec


def test_spans_record_order_offsets_and_durations() -> None:
    r = rec.Recorder("request", "POST /v1/search")
    with rec.activate(r):
        with rec.span("auth"):
            time.sleep(0.005)
        with rec.span("lexical", candidates_total=3) as handle:
            handle.set(candidates=[{"id": "a", "score": 1.0, "rank": 1}])
    row = r.finish()
    names = [s["name"] for s in row["spans"]]
    assert names == ["auth", "lexical"]
    assert [s["seq"] for s in row["spans"]] == [0, 1]
    assert row["spans"][0]["duration_ms"] >= 4
    assert row["spans"][1]["started_offset_ms"] >= row["spans"][0]["duration_ms"]
    assert row["spans"][1]["detail"]["candidates_total"] == 3
    assert row["spans"][1]["detail"]["candidates"][0]["id"] == "a"
    assert row["status"] == "ok"
    assert row["duration_ms"] >= 5


def test_exception_marks_span_error_and_reraises() -> None:
    r = rec.Recorder("job", "enrich_memory")
    with rec.activate(r), pytest.raises(ValueError):
        with rec.span("embed"):
            raise ValueError("boom")
    row = r.finish()
    assert row["spans"][0]["status"] == "error"
    assert row["spans"][0]["detail"]["error"] == "ValueError"
    assert row["status"] == "error"


def test_span_is_noop_without_active_recorder() -> None:
    assert rec.current_recorder() is None
    with rec.span("vector") as handle:
        handle.set(candidates=[])
    assert rec.current_recorder() is None


def test_detail_is_capped() -> None:
    r = rec.Recorder("request", "POST /v1/search")
    with rec.activate(r):
        with rec.span("vector") as handle:
            handle.set(
                candidates=[{"id": f"{i:032x}", "score": 0.5, "rank": i} for i in range(400)]
            )
            handle.set(blob="x" * 20_000)
    row = r.finish()
    detail = row["spans"][0]["detail"]
    assert len(detail["candidates"]) == rec.CANDIDATE_LIMIT
    assert len(json.dumps(detail)) <= rec.SPAN_DETAIL_LIMIT
    assert detail["truncated"] is True


def test_summary_query_is_capped_and_identity_recorded() -> None:
    r = rec.Recorder("request", "POST /v1/search")
    r.note(query="q" * 2000, hits=3)
    r.set_identity(organization_id="org", principal_id="p")
    row = r.finish(status="degraded")
    assert len(row["summary"]["query"]) == 500
    assert row["summary"]["hits"] == 3
    assert row["organization_id"] == "org"
    assert row["status"] == "degraded"


def test_exception_detail_never_carries_the_message() -> None:
    """Exception text can embed SQL parameters or constraint values; keep the type only."""
    r = rec.Recorder("job", "enrich_memory")
    with rec.activate(r), pytest.raises(RuntimeError):
        with rec.span("write_edges"):
            raise RuntimeError("DETAIL: Key (canonical_name)=(secret-content-marker)")
    detail = r.finish()["spans"][0]["detail"]
    assert detail["error"] == "RuntimeError"
    assert "secret-content-marker" not in str(detail)
