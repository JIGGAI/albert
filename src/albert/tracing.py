"""Wire the recorder around HTTP requests and worker jobs."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from albert.models import Job
from albert.recorder import Recorder, activate
from albert.trace_writer import get_trace_writer

logger = logging.getLogger("albert.tracing")
UNTRACED_PATH_PREFIXES = ("/v1/health", "/docs", "/openapi.json", "/redoc")


class RecordingMiddleware:
    """Pure ASGI middleware: one Recorder per request, written in a finally."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("path", "").startswith(UNTRACED_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return
        recorder = Recorder("request", f"{scope['method']} {scope['path']}")

        async def recording_send(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                recorder.http_status = message["status"]
            await send(message)

        with activate(recorder):
            try:
                await self.app(scope, receive, recording_send)
            except Exception:
                recorder.http_status = recorder.http_status or 500
                raise
            finally:
                route = scope.get("route")
                template = getattr(route, "path", None)
                if template:
                    recorder.name = f"{scope['method']} {template}"
                status = recorder.status
                code = recorder.http_status or 500
                if code >= 400:
                    status = "error"
                try:
                    get_trace_writer().submit(recorder.finish(status=status or "ok"))
                except Exception:  # recording never fails a request
                    logger.exception("trace submit failed")


@contextmanager
def traced_job(job: Job) -> Iterator[Recorder]:
    recorder = Recorder("job", job.job_type)
    recorder.set_identity(job.organization_id, None)
    recorder.note(job_id=str(job.id), attempt=job.attempts, payload=dict(job.payload))
    with activate(recorder):
        try:
            yield recorder
        except Exception:
            _submit(recorder.finish(status="error"))
            raise
        _submit(recorder.finish())


def _submit(row: dict) -> None:
    try:
        get_trace_writer().submit(row)
    except Exception:  # recording never fails a job
        logger.exception("trace submit failed")
