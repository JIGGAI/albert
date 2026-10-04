from __future__ import annotations

import random

from albert.clusters import cluster_nodes, name_clusters

A = [f"a{i}" for i in range(5)]
B = [f"b{i}" for i in range(4)]


def _dense(group: list[str], weight: float = 0.9) -> list[tuple[str, str, float]]:
    return [
        (first, second, weight)
        for index, first in enumerate(group)
        for second in group[index + 1 :]
    ]


def _two_groups() -> tuple[list[str], list[tuple[str, str, float]]]:
    return A + B, [*_dense(A), *_dense(B), ("a4", "b0", 0.79)]


def test_two_dense_groups_joined_by_a_weak_link_are_two_clusters() -> None:
    nodes, links = _two_groups()
    assignment = cluster_nodes(nodes, links)
    assert {assignment[node] for node in A} == {0}  # the larger group is cluster 0
    assert {assignment[node] for node in B} == {1}


def test_clustering_is_deterministic_across_runs_and_input_order() -> None:
    nodes, links = _two_groups()
    expected = cluster_nodes(nodes, links)
    shuffler = random.Random(7)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        shuffled_links = [(b, a, w) if shuffler.random() < 0.5 else (a, b, w) for a, b, w in links]
        shuffler.shuffle(shuffled_nodes)
        shuffler.shuffle(shuffled_links)
        assert cluster_nodes(shuffled_nodes, shuffled_links) == expected


def test_small_groups_and_isolated_nodes_are_unclustered() -> None:
    nodes = [*A, "p1", "p2", "alone"]
    assignment = cluster_nodes(nodes, [*_dense(A), ("p1", "p2", 0.95)])
    assert set(assignment) == set(A)
    assert cluster_nodes(["x", "y"], [("x", "y", 0.9)]) == {}
    assert cluster_nodes([], []) == {}


def test_equal_sized_clusters_are_ordered_by_smallest_member() -> None:
    first, second = ["m1", "m2", "m3"], ["k1", "k2", "k3"]
    assignment = cluster_nodes(first + second, [*_dense(first), *_dense(second)])
    assert assignment["k1"] == 0 and assignment["m1"] == 1


def test_links_to_unknown_nodes_are_ignored() -> None:
    assignment = cluster_nodes(A, [*_dense(A), ("a0", "ghost", 0.99)])
    assert set(assignment) == set(A)


def test_names_use_distinctive_terms() -> None:
    titles = {
        "a0": "Brand voice for the social team",
        "a1": "Brand voice rules 2026",
        "a2": "The brand voice and tone",
        "b0": "YOT revenue sync for the team",
        "b1": "Revenue sync backfill 11082",
        "b2": "YOT revenue sync is resumable",
    }
    assignment = {"a0": 0, "a1": 0, "a2": 0, "b0": 1, "b1": 1, "b2": 1}
    names = name_clusters(titles, assignment)
    assert names == {0: "Brand Voice", 1: "Revenue Sync"}


def test_names_never_use_stop_words_or_numbers_and_fall_back() -> None:
    titles = {"a": "The 2026 and 11082", "b": "for the 42", "c": "", "d": "Payout Payout"}
    names = name_clusters(titles, {"a": 0, "b": 0, "c": 0, "d": 1})
    assert names[0] == "Cluster 1"
    assert names[1] == "Payout"


def test_cluster_names_are_unique() -> None:
    titles = {"a": "Payout export", "b": "Payout export", "c": "Payout export"}
    names = name_clusters(titles, {"a": 0, "b": 1, "c": 2})
    assert len(set(names.values())) == 3


def test_names_skip_note_prefixes_dates_and_timezones() -> None:
    titles = {
        "a": "reference_yot_roster_vs_report_names",
        "b": "reference yot revenue backfill",
        "c": "reference_yot_holiday_location_scoping",
        "d": "Lead Triage Loop - 2026-03-28 03:32 UTC",
        "e": "Lead Triage Log — 2026-03-27 22:03 UTC",
        "f": "Lead Triage Execution - 14:32 UTC (Friday, March 27th, 2026)",
        "g": "Daily Memory - March 28, 2026",
        "h": "Daily Memory - March 27, 2026",
        "i": "Daily Log - March 26, 2026",
    }
    assignment = {"a": 0, "b": 0, "c": 0, "d": 1, "e": 1, "f": 1, "g": 2, "h": 2, "i": 2}
    names = name_clusters(titles, assignment)
    # One strong term beats padding the name with a word few members share.
    assert names[0] == "YOT"
    assert names[1] == "Lead Triage"
    assert names[2] == "Daily Memory"


def test_a_shared_subject_word_can_name_a_cluster() -> None:
    titles = {
        "a": "Team Memory Policy (File-first)",
        "b": "Memory Policy — hmx-social-team",
        "c": "Team Memory Policy (File-first)",
        "d": "Goals folder — hmx-social-team",
        "e": "Goals — hmx-marketing-team",
        "f": "Goals folder — hmx-marketing-team",
    }
    names = name_clusters(titles, {"a": 0, "b": 0, "c": 0, "d": 1, "e": 1, "f": 1})
    assert names[0] == "Memory Policy"
    assert names[1].startswith("Goals")
