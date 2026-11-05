"""Keyword search keeps only a long query's rarest terms; the cap is a setting."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from albert import search
from albert.config import Settings


class _Session:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    def execute(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self.counts.items()))


def test_default_cap_is_eight() -> None:
    assert Settings(_env_file=None).lexical_max_terms == 8


def test_rarest_terms_respect_the_configured_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = {"deploy": 3, "hotfix": 1, "please": 900, "today": 400, "vpn": 2, "absent": 0}
    monkeypatch.setattr(search, "get_settings", lambda: SimpleNamespace(lexical_max_terms=3))

    kept = search._rarest_lexemes(_Session(counts), list(counts), uuid4())

    assert kept == ["hotfix", "vpn", "deploy"]


def test_short_queries_are_not_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "get_settings", lambda: SimpleNamespace(lexical_max_terms=8))

    kept = search._rarest_lexemes(_Session({}), ["deploy", "hotfix"], uuid4())

    assert kept == ["deploy", "hotfix"]
