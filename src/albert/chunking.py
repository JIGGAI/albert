"""Split memory text into bounded, overlapping chunks for embedding.

Embedding models truncate silently or reject long inputs, so every memory is
indexed as a sequence of chunks. Chunks follow paragraph and sentence
boundaries where possible and fall back to a hard split for unbroken runs.
"""

from __future__ import annotations

import re

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


def _units(text: str, max_characters: int, overlap_characters: int) -> list[str]:
    """Break text into pieces that each fit the budget, preferring natural breaks."""
    units: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_characters:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_BREAK.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_characters:
                units.append(sentence)
                continue
            step = max(1, max_characters - overlap_characters)
            for start in range(0, len(sentence), step):
                units.append(sentence[start : start + max_characters])
                if start + max_characters >= len(sentence):
                    break
    return units


def _tail(text: str, overlap_characters: int) -> str:
    if overlap_characters <= 0 or len(text) <= overlap_characters:
        return ""
    tail = text[-overlap_characters:]
    cut = tail.find(" ")
    return tail[cut + 1 :].strip() if cut != -1 else tail


def chunk_text(text: str, *, max_characters: int, overlap_characters: int) -> list[str]:
    """Return non-empty chunks of at most ``max_characters`` each.

    Consecutive chunks share up to ``overlap_characters`` of trailing context so
    a fact straddling a boundary is still embedded whole in one of them.
    """
    if max_characters <= 0:
        raise ValueError("max_characters must be positive")
    overlap_characters = max(0, min(overlap_characters, max_characters // 2))
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_characters:
        return [text]

    chunks: list[str] = []
    current = ""
    for unit in _units(text, max_characters, overlap_characters):
        if not current:
            current = unit
            continue
        if len(current) + 2 + len(unit) <= max_characters:
            current = f"{current}\n\n{unit}"
            continue
        chunks.append(current)
        carry = _tail(current, overlap_characters)
        fits = bool(carry) and len(carry) + 2 + len(unit) <= max_characters
        current = f"{carry}\n\n{unit}" if fits else unit
    if current:
        chunks.append(current)
    return chunks
