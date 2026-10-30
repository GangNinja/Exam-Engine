"""Small graph algorithms shared by prerequisite discovery and path planning.

These helpers operate on Phase 5 ``GraphEdge`` records only. They never write,
merge or delete anything, so validation can be called at any time without
changing the graph.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from exam_engine.models.graph import GraphEdge, GraphNode
from exam_engine.models.prerequisite import ConceptRef, PrerequisiteConflict, PrerequisiteCycle

_MAX_CYCLES = 50


def concept_ref(node: GraphNode) -> ConceptRef:
    """Build the shared concept reference, retaining source scope when present."""
    provenance: dict[str, object] = {}
    for key in (
        "entity_id",
        "unit_ids",
        "document_ids",
        "aliases",
        "source_concept_ids",
        "origins",
        "verified",
    ):
        value = node.metadata.get(key)
        if value:
            provenance[key] = value
    if node.entity_id:
        provenance.setdefault("entity_id", node.entity_id)
    return ConceptRef(
        concept_id=node.node_id,
        label=node.label,
        course_id=node.course_id,
        syllabus_id=node.syllabus_id,
        provenance=provenance,
    )


def edge_label_map(edges: Iterable[GraphEdge], labels: dict[str, str]) -> dict[str, str]:
    """Fill missing labels deterministically rather than creating fake names."""
    result = dict(labels)
    for edge in edges:
        result.setdefault(edge.source_node_id, edge.source_node_id)
        result.setdefault(edge.target_node_id, edge.target_node_id)
    return result


def find_cycles(
    edges: Iterable[GraphEdge], labels: dict[str, str], *, limit: int = _MAX_CYCLES
) -> list[PrerequisiteCycle]:
    """Find directed cycles with an iterative DFS.

    Each cycle reports its unique concepts and the edges that close the loop.
    Traversal is bounded, deterministic and never recursive, so a cyclic graph
    cannot exhaust the call stack or loop forever.
    """
    edge_list = sorted(
        edges,
        key=lambda edge: (edge.source_node_id, edge.target_node_id, edge.edge_id),
    )
    adjacency: dict[str, list[GraphEdge]] = defaultdict(list)
    nodes: set[str] = set()
    for edge in edge_list:
        adjacency[edge.source_node_id].append(edge)
        nodes.add(edge.source_node_id)
        nodes.add(edge.target_node_id)
    for values in adjacency.values():
        values.sort(key=lambda edge: (edge.target_node_id, edge.edge_id))

    color: dict[str, int] = {}
    cycles: list[PrerequisiteCycle] = []
    seen_cycles: set[tuple[str, ...]] = set()

    for start in sorted(nodes):
        if color.get(start, 0) != 0:
            continue
        color[start] = 1
        path = [start]
        path_edges: list[str] = []
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, index = stack[-1]
            outgoing = adjacency.get(node, [])
            if index >= len(outgoing):
                color[node] = 2
                stack.pop()
                path.pop()
                if path_edges:
                    path_edges.pop()
                continue
            edge = outgoing[index]
            stack[-1] = (node, index + 1)
            target = edge.target_node_id
            target_color = color.get(target, 0)
            if target_color == 1:
                try:
                    start_index = path.index(target)
                except ValueError:
                    continue
                cycle_nodes = path[start_index:]
                cycle_edges = [*path_edges[start_index:], edge.edge_id]
                key = tuple(sorted(cycle_edges))
                if key in seen_cycles:
                    continue
                seen_cycles.add(key)
                cycles.append(
                    PrerequisiteCycle(
                        concept_ids=cycle_nodes,
                        labels=[labels.get(item, item) for item in cycle_nodes],
                        edge_ids=cycle_edges,
                        message=_cycle_message(cycle_nodes, labels),
                    )
                )
                if len(cycles) >= limit:
                    return sorted(cycles, key=lambda item: tuple(item.concept_ids))
            elif target_color == 0:
                color[target] = 1
                path.append(target)
                path_edges.append(edge.edge_id)
                stack.append((target, 0))
    return sorted(cycles, key=lambda item: tuple(item.concept_ids))


def find_reverse_conflicts(
    edges: Iterable[GraphEdge], labels: dict[str, str]
) -> list[PrerequisiteConflict]:
    """Report A->B / B->A pairs instead of silently deleting either direction."""
    by_pair = {(edge.source_node_id, edge.target_node_id): edge for edge in edges}
    conflicts: list[PrerequisiteConflict] = []
    seen: set[tuple[str, str]] = set()
    for (source, target), edge in by_pair.items():
        reverse = by_pair.get((target, source))
        if reverse is None:
            continue
        key = (source, target) if source < target else (target, source)
        if key in seen:
            continue
        seen.add(key)
        left = labels.get(source, source)
        right = labels.get(target, target)
        conflicts.append(
            PrerequisiteConflict(
                kind="order_conflict",
                concept_ids=[source, target],
                edge_ids=sorted([edge.edge_id, reverse.edge_id]),
                message=(
                    f"conflicting directions: '{left}' is a prerequisite of '{right}' "
                    f"and vice versa; both records are preserved for review"
                ),
                blocking=False,
                resolution="review both edges; disable or downgrade the unsupported one",
            )
        )
    return sorted(conflicts, key=lambda item: tuple(item.edge_ids))


def _cycle_message(concepts: list[str], labels: dict[str, str]) -> str:
    names = [labels.get(item, item) for item in concepts]
    if names:
        names.append(names[0])
    return "cycle: " + " -> ".join(names)


__all__ = ["concept_ref", "edge_label_map", "find_cycles", "find_reverse_conflicts"]
