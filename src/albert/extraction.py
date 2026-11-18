"""Bounded, tenant-safe multimodal extraction."""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

CONTRACT_VERSION = "albert.extraction/v1"
SUPPORTED_MIMES = {"image/png", "image/jpeg", "application/pdf"}


class ExtractionError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ExtractionLimits:
    max_file_bytes: int = 25 * 1024 * 1024
    max_pages: int = 100
    max_pixels_per_page: int = 40_000_000
    max_seconds: float = 120.0
    max_output_chars: int = 2_000_000
    pdf_dpi: int = 200


@dataclass(frozen=True)
class Artifact:
    organization_id: str
    artifact_id: str
    filename: str
    content: bytes
    declared_mime: str | None = None
    sensitivity: str = "internal"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class ExtractedSegment:
    id: str
    organization_id: str
    artifact_id: str
    artifact_digest: str
    page: int
    region: Region
    text: str
    confidence: float
    extractor: str
    extractor_version: str
    contract_version: str = CONTRACT_VERSION
    trust: Literal["untrusted"] = "untrusted"
    sensitivity: str = "internal"

    def recall_text(self) -> str:
        """Render OCR as delimited data, never as executable instructions."""
        return (
            f'<untrusted-ocr artifact="{self.artifact_id}" page="{self.page}">\n'
            f"{self.text}\n</untrusted-ocr>"
        )


ResultStatus = Literal[
    "completed",
    "unsupported",
    "corrupt",
    "encrypted",
    "quarantined",
    "cancelled",
    "resource_limited",
    "provider_unavailable",
]


@dataclass(frozen=True)
class ExtractionResult:
    organization_id: str
    artifact_id: str
    artifact_digest: str
    status: ResultStatus
    mime_type: str | None
    segments: tuple[ExtractedSegment, ...] = ()
    message: str = ""
    contract_version: str = CONTRACT_VERSION


@dataclass(frozen=True)
class OCRWord:
    page: int
    region: Region
    text: str
    confidence: float


class OCRAdapter(Protocol):
    name: str
    version: str

    def extract(
        self,
        artifact: Artifact,
        mime_type: str,
        limits: ExtractionLimits,
        cancelled: Callable[[], bool],
    ) -> Sequence[OCRWord]: ...


def detected_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def image_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        if len(content) < 24 or content[12:16] != b"IHDR":
            raise ExtractionError("corrupt", "PNG has no valid IHDR")
        return struct.unpack(">II", content[16:24])
    if mime_type == "image/jpeg":
        offset = 2
        while offset + 9 <= len(content):
            if content[offset] != 0xFF:
                raise ExtractionError("corrupt", "JPEG marker stream is invalid")
            marker = content[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(content):
                break
            length = int.from_bytes(content[offset : offset + 2], "big")
            if length < 2 or offset + length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                height = int.from_bytes(content[offset + 3 : offset + 5], "big")
                width = int.from_bytes(content[offset + 5 : offset + 7], "big")
                return width, height
            offset += length
        raise ExtractionError("corrupt", "JPEG has no valid frame")
    raise ExtractionError("unsupported", "Unsupported image format")


def check_image_bounds(content: bytes, mime_type: str, limits: ExtractionLimits) -> None:
    width, height = image_dimensions(content, mime_type)
    if width <= 0 or height <= 0 or width * height > limits.max_pixels_per_page:
        raise ExtractionError("resource_limited", "Image pixel limit exceeded")


def run_bounded(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command, capture_output=True, check=False, timeout=max(timeout, 0.01)
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("resource_limited", "Extraction time limit exceeded") from exc
    if len(result.stdout) + len(result.stderr) > 4_000_000:
        raise ExtractionError("resource_limited", "Extractor output limit exceeded")
    return result


def parse_tsv(data: bytes, page: int) -> list[OCRWord]:
    words: list[OCRWord] = []
    for line in data.decode("utf-8", errors="replace").splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) != 12 or not fields[11].strip():
            continue
        try:
            x, y, width, height = map(int, fields[6:10])
            confidence = max(0.0, min(1.0, float(fields[10]) / 100.0))
        except ValueError:
            continue
        words.append(OCRWord(page, Region(x, y, width, height), fields[11].strip(), confidence))
    return words


@dataclass(frozen=True)
class TesseractOCR:
    """Self-hosted OCR using Tesseract and Poppler command-line tools."""

    name: str = "tesseract"
    version: str = "5"
    language: str = "eng"

    def _ocr_page(self, path: Path, page: int, remaining: float) -> list[OCRWord]:
        result = run_bounded(
            ["tesseract", str(path), "stdout", "-l", self.language, "tsv"], timeout=remaining
        )
        if result.returncode != 0:
            raise ExtractionError("corrupt", "OCR provider rejected the rendered page")
        return parse_tsv(result.stdout, page)

    def extract(
        self,
        artifact: Artifact,
        mime_type: str,
        limits: ExtractionLimits,
        cancelled: Callable[[], bool],
    ) -> Sequence[OCRWord]:
        required = ["tesseract"]
        if mime_type == "application/pdf":
            required += ["pdfinfo", "pdftoppm"]
        if any(shutil.which(tool) is None for tool in required):
            raise ExtractionError("provider_unavailable", "Required OCR tools are unavailable")
        started = time.monotonic()

        def remaining() -> float:
            return limits.max_seconds - (time.monotonic() - started)

        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}[
            mime_type
        ]
        with tempfile.TemporaryDirectory(prefix="albert-ocr-") as directory:
            source = Path(directory) / f"artifact{suffix}"
            source.write_bytes(artifact.content)
            if mime_type != "application/pdf":
                check_image_bounds(artifact.content, mime_type, limits)
                if cancelled():
                    raise ExtractionError("cancelled", "Extraction cancelled")
                return self._ocr_page(source, 1, remaining())

            info = run_bounded(["pdfinfo", str(source)], timeout=remaining())
            text = info.stdout.decode("utf-8", errors="replace")
            diagnostics = (text + info.stderr.decode("utf-8", errors="replace")).lower()
            if info.returncode != 0:
                status = (
                    "encrypted"
                    if "password" in diagnostics or "encrypted" in diagnostics
                    else "corrupt"
                )
                raise ExtractionError(status, "PDF cannot be safely inspected")
            page_match = re.search(r"^Pages:\s+(\d+)$", text, re.MULTILINE)
            encrypted = re.search(r"^Encrypted:\s+(\w+)", text, re.MULTILINE | re.IGNORECASE)
            if encrypted and encrypted.group(1).lower() != "no":
                raise ExtractionError("encrypted", "Encrypted PDFs are not supported")
            if not page_match:
                raise ExtractionError("corrupt", "PDF page count is unavailable")
            pages = int(page_match.group(1))
            if pages < 1 or pages > limits.max_pages:
                raise ExtractionError("resource_limited", "PDF page limit exceeded")

            words: list[OCRWord] = []
            for page in range(1, pages + 1):
                if cancelled():
                    raise ExtractionError("cancelled", "Extraction cancelled")
                prefix = Path(directory) / f"page-{page}"
                rendered = run_bounded(
                    [
                        "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                        "-r", str(limits.pdf_dpi), "-png", str(source), str(prefix),
                    ],
                    timeout=remaining(),
                )
                if rendered.returncode != 0:
                    raise ExtractionError("corrupt", "PDF page rendering failed")
                page_path = prefix.with_suffix(".png")
                check_image_bounds(page_path.read_bytes(), "image/png", limits)
                words.extend(self._ocr_page(page_path, page, remaining()))
            return words


@dataclass
class ExtractionService:
    adapter: OCRAdapter = field(default_factory=TesseractOCR)
    limits: ExtractionLimits = field(default_factory=ExtractionLimits)

    def extract(
        self, artifact: Artifact, *, cancelled: Callable[[], bool] = lambda: False
    ) -> ExtractionResult:
        mime_type = detected_mime(artifact.content)
        if len(artifact.content) > self.limits.max_file_bytes:
            return self._failure(
                artifact, mime_type, "resource_limited", "File size limit exceeded"
            )
        if mime_type is None:
            return self._failure(artifact, None, "unsupported", "Unsupported artifact format")
        if artifact.declared_mime and artifact.declared_mime != mime_type:
            return self._failure(artifact, mime_type, "quarantined", "MIME does not match content")
        try:
            words = self.adapter.extract(artifact, mime_type, self.limits, cancelled)
            if cancelled():
                raise ExtractionError("cancelled", "Extraction cancelled")
            if sum(len(word.text) for word in words) > self.limits.max_output_chars:
                raise ExtractionError("resource_limited", "Extracted text limit exceeded")
            segments = tuple(self._segment(artifact, word) for word in words)
            return ExtractionResult(
                artifact.organization_id,
                artifact.artifact_id,
                artifact.digest,
                "completed",
                mime_type,
                segments,
            )
        except ExtractionError as exc:
            return self._failure(artifact, mime_type, exc.status, str(exc))

    def _segment(self, artifact: Artifact, word: OCRWord) -> ExtractedSegment:
        identity = "|".join(
            (
                artifact.organization_id, artifact.artifact_id, artifact.digest, str(word.page),
                f"{word.region.x},{word.region.y},{word.region.width},{word.region.height}",
                self.adapter.name, self.adapter.version, word.text,
            )
        )
        return ExtractedSegment(
            id=hashlib.sha256(identity.encode()).hexdigest(),
            organization_id=artifact.organization_id,
            artifact_id=artifact.artifact_id,
            artifact_digest=artifact.digest,
            page=word.page,
            region=word.region,
            text=word.text,
            confidence=word.confidence,
            extractor=self.adapter.name,
            extractor_version=self.adapter.version,
            sensitivity=artifact.sensitivity,
        )

    @staticmethod
    def _failure(
        artifact: Artifact, mime_type: str | None, status: str, message: str
    ) -> ExtractionResult:
        return ExtractionResult(  # type: ignore[arg-type]
            artifact.organization_id,
            artifact.artifact_id,
            artifact.digest,
            status,
            mime_type,
            message=message,
        )


class ExtractionIndex:
    """Atomic publication boundary; production adapters use identical scoped keys."""

    def __init__(self) -> None:
        self._segments: dict[tuple[str, str], tuple[ExtractedSegment, ...]] = {}

    def publish(self, result: ExtractionResult) -> bool:
        if result.status != "completed":
            return False
        if any(segment.organization_id != result.organization_id for segment in result.segments):
            return False
        self._segments[(result.organization_id, result.artifact_id)] = result.segments
        return True

    def get(self, organization_id: str, artifact_id: str) -> tuple[ExtractedSegment, ...] | None:
        return self._segments.get((organization_id, artifact_id))

    def search(self, organization_id: str, query: str) -> tuple[ExtractedSegment, ...]:
        terms = query.casefold().split()
        if not terms:
            return ()
        return tuple(
            segment
            for (tenant, _), segments in self._segments.items()
            if tenant == organization_id
            for segment in segments
            if all(term in segment.text.casefold() for term in terms)
        )

    def delete(self, organization_id: str, artifact_id: str) -> bool:
        return self._segments.pop((organization_id, artifact_id), None) is not None
