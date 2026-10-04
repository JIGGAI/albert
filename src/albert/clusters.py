"""Deterministic clustering of the memory map and names for the clusters.

Pure functions over ids, link weights and titles: no database, no randomness,
so the same snapshot always yields the same clusters in the same order.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

MAX_ROUNDS = 20
NAME_TERMS = 2
_TERM = re.compile(r"[a-z][a-z0-9'-]*")
STOP_WORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before
    being below between both but by can could did do does doing done down during each few for
    from further get gets got had has have having he her here hers him his how i if in into is
    it its just may me more most must my no nor not now of off on once one only or other our
    out over own per same she should so some such than that the their them then there these
    they this those through to too under until up use used uses using very via was we were
    what when where which while who why will with without would you your
    new note notes memory memories update updated updates misc general
    """.split()
)


def cluster_nodes(
    node_ids: list[str], links: list[tuple[str, str, float]], *, min_size: int = 3
) -> dict[str, int]:
    """Assign nodes to clusters by weighted label propagation.

    Nodes are visited in id order and adopt the label carrying the most link
    weight among their neighbours (ties go to the smallest label). Clusters are
    numbered by descending size, then smallest member id; nodes in groups
    smaller than `min_size` are left out of the result.
    """
    known = set(node_ids)
    neighbours: dict[str, dict[str, float]] = defaultdict(dict)
    for first, second, weight in links:
        if first == second or first not in known or second not in known:
            continue
        strength = max(float(weight), 0.0)
        neighbours[first][second] = max(neighbours[first].get(second, 0.0), strength)
        neighbours[second][first] = max(neighbours[second].get(first, 0.0), strength)
    order = sorted(neighbours)
    labels = {node: node for node in order}
    for _ in range(MAX_ROUNDS):
        changed = False
        for node in order:
            totals: dict[str, float] = defaultdict(float)
            for other, strength in neighbours[node].items():
                totals[labels[other]] += strength
            best = min(totals, key=lambda label: (-round(totals[label], 9), label))
            if best != labels[node]:
                labels[node] = best
                changed = True
        if not changed:
            break
    groups: dict[str, list[str]] = defaultdict(list)
    for node, label in labels.items():
        groups[label].append(node)
    kept = sorted(
        (sorted(members) for members in groups.values() if len(members) >= min_size),
        key=lambda members: (-len(members), members[0]),
    )
    return {node: index for index, members in enumerate(kept) for node in members}


def _terms(title: str) -> list[str]:
    """Distinct naming terms of a title, in the order they appear."""
    terms: list[str] = []
    for term in _TERM.findall(title.lower()):
        if len(term) >= 3 and term not in STOP_WORDS and term not in terms:
            terms.append(term)
    return terms


def _display(term: str) -> str:
    return term.upper() if len(term) <= 3 else term.capitalize()


def name_clusters(titles: dict[str, str], assignment: dict[str, int]) -> dict[int, str]:
    """Name each cluster by its most distinctive title terms (TF-IDF across clusters)."""
    clusters = sorted(set(assignment.values()))
    frequency: dict[int, Counter[str]] = {cluster: Counter() for cluster in clusters}
    sizes: Counter[int] = Counter(assignment.values())
    position: dict[int, dict[str, int]] = {cluster: defaultdict(int) for cluster in clusters}
    for node, cluster in assignment.items():
        terms = _terms(titles.get(node, ""))
        frequency[cluster].update(terms)
        for index, term in enumerate(terms):
            position[cluster][term] += index
    spread: Counter[str] = Counter()
    for counts in frequency.values():
        spread.update(counts.keys())
    names: dict[int, str] = {}
    taken: set[str] = set()
    for cluster in clusters:
        scored = sorted(
            (
                (
                    (count / sizes[cluster]) * (1.0 + math.log((1 + len(clusters)) / spread[term])),
                    term,
                )
                for term, count in frequency[cluster].items()
            ),
            key=lambda item: (-round(item[0], 9), item[1]),
        )
        ranked = [term for _score, term in scored]
        name = ""
        # Prefer the two strongest terms; widen only to keep names distinct.
        for width in range(min(NAME_TERMS, len(ranked)), len(ranked) + 1):
            # Read like the titles do: order the chosen terms by where they usually sit.
            chosen = sorted(
                ranked[:width],
                key=lambda term: (position[cluster][term] / frequency[cluster][term], term),
            )
            candidate = " ".join(_display(term) for term in chosen)
            if candidate and candidate not in taken:
                name = candidate
                break
        if not name:
            name = f"Cluster {cluster + 1}"
        taken.add(name)
        names[cluster] = name
    return names
