"""Explainable learning-path generation over Phase 6 prerequisite edges.

The planner is a dependency ordering, not a claim of pedagogical optimality. It
uses only relationships that pass the request's status policy, records cycles
instead of looping, and keeps candidate relationships visibly uncertain.
"""

from __future__ import annotations

import heapq
from collections import defaultdict, deque

from exam_engine.config import PrerequisiteSettings
from exam_engine.graph.service import GraphService
from exam_engine.models.enums import GraphEdgeStatus, GraphNodeType
from exam_engine.models.graph import GraphEdge, GraphNode
from exam_engine.models.prerequisite import (
    ConceptRef,
    LearningPathPlan,
    LearningPathRequest,
    LearningPathStatus,
    LearningPathStep,
    PathStepRole,
    PrerequisiteCycle,
    PrerequisiteEvidence,
)
from exam_engine.prerequisite.discovery import _evidence_from_edge
from exam_engine.prerequisite.graph_ops import (
    concept_ref as make_concept_ref,
)
from exam_engine.prerequisite.graph_ops import (
    find_cycles,
    find_reverse_conflicts,
)
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.repositories import CourseRepository, SyllabusRepository

ALGORITHM = "phase6_learning_path"
ALGORITHM_VERSION = "phase6.v1"

_STATUS_PRIORITY: dict[GraphEdgeStatus, int] = {
    GraphEdgeStatus.CANDIDATE: 0,
    GraphEdgeStatus.INFERRED: 1,
    GraphEdgeStatus.EXPLICIT: 2,
    GraphEdgeStatus.REVIEWED: 3,
    GraphEdgeStatus.VERIFIED: 4,
}


class LearningPathService:
    """Generate deterministic plans for a concept, topic or syllabus unit."""

    def __init__(self, graph: GraphService, settings: PrerequisiteSettings | None = None) -> None:
        self.graph = graph
        self.settings = settings or PrerequisiteSettings()

    def generate(self, request: LearningPathRequest) -> LearningPathPlan:
        plan = LearningPathPlan(
            course_id=request.course_id,
            syllabus_id=request.syllabus_id,
            target_node_id=request.target_node_id,
            target_node_type=request.target_node_type,
            status=LearningPathStatus.INVALID_TARGET.value,
            algorithm=ALGORITHM,
            algorithm_version=ALGORITHM_VERSION,
            config={
                **request.model_dump(mode="json"),
                "path_max_depth_default": self.settings.path_max_depth,
            },
            limitations=[
                "Ordering is dependency-based; it is not evidence of pedagogical optimality.",
                "Candidate and inferred relationships are never treated as verified facts.",
                "Known concepts are explicit input; activity alone never proves mastery.",
            ],
        )
        with self.graph.db.session() as session:
            if CourseRepository(session).get(request.course_id) is None:
                raise KeyError(f"unknown course: {request.course_id}")
            course_syllabi = SyllabusRepository(session).get_by_course(request.course_id)
            graph_edges = GraphRepository(session).list_edges(course_id=request.course_id)
        try:
            target = self.graph.node(request.target_node_id)
        except KeyError:
            plan.missing_metadata.append(f"unknown target node: {request.target_node_id}")
            return plan
        if target.node_type != request.target_node_type:
            plan.missing_metadata.append(
                f"target {target.node_id} is a {target.node_type.value}, "
                f"not a {request.target_node_type.value}"
            )
            return plan
        if target.course_id not in (None, request.course_id):
            plan.missing_metadata.append(
                f"target {target.node_id} belongs to {target.course_id}, not {request.course_id}"
            )
            return plan
        if target.course_id is None:
            plan.missing_metadata.append(
                "target node is shared across courses; course context is the requested course"
            )

        all_concept_nodes = self.graph.concepts(request.course_id)
        all_concepts = {node.node_id: node for node in all_concept_nodes}
        labels = {node_id: node.label for node_id, node in all_concepts.items()}
        course_syllabus_ids = {item.syllabus_id for item in course_syllabi}
        course_syllabus_ids.update(
            node.syllabus_id for node in all_concept_nodes if node.syllabus_id is not None
        )
        course_syllabus_ids.update(
            edge.syllabus_id for edge in graph_edges if edge.syllabus_id is not None
        )
        if target.syllabus_id is not None:
            course_syllabus_ids.add(target.syllabus_id)
        mixed_versions = len(course_syllabus_ids) > 1

        requested_syllabus = request.syllabus_id
        if requested_syllabus is not None and requested_syllabus not in course_syllabus_ids:
            plan.missing_metadata.append(
                f"unknown syllabus {requested_syllabus} for course {request.course_id}"
            )
            return plan
        if (
            requested_syllabus is not None
            and target.syllabus_id is not None
            and requested_syllabus != target.syllabus_id
        ):
            plan.missing_metadata.append(
                f"target is scoped to syllabus {target.syllabus_id}, "
                f"not requested syllabus {requested_syllabus}"
            )
            return plan
        effective_syllabus = requested_syllabus or target.syllabus_id
        if requested_syllabus is None and target.syllabus_id is not None:
            plan.limitations.append(
                "syllabus version was taken from the target node; pass --syllabus to override"
            )
        if requested_syllabus is None and target.syllabus_id is None:
            plan.missing_metadata.append(
                "target has no syllabus scope; course-level prerequisite relationships were used"
            )

        composed_targets, composition_note = self._resolve_targets(
            target, request, effective_syllabus, mixed_versions
        )
        if composition_note:
            plan.limitations.append(composition_note)
        if not composed_targets:
            plan.limitations.append(
                "no concepts could be composed for this target; no supported plan exists"
            )
            plan.status = LearningPathStatus.INSUFFICIENT_EVIDENCE.value
            plan.syllabus_id = effective_syllabus
            return plan
        plan.target_concepts = [self._ref(node) for node in composed_targets]
        plan.status = LearningPathStatus.OK.value
        plan.syllabus_id = effective_syllabus

        for node in composed_targets:
            all_concepts.setdefault(node.node_id, node)
            labels.setdefault(node.node_id, node.label)
        max_depth = request.max_depth
        if "max_depth" not in request.model_fields_set:
            max_depth = self.settings.path_max_depth
        max_depth = max(1, min(32, max_depth))
        known_ids, known_nodes = self._validate_known(
            request,
            all_concepts,
            effective_syllabus,
            plan,
            mixed_versions=mixed_versions,
        )

        edges = self.graph.prerequisite_edges(
            request.course_id,
            syllabus_id=effective_syllabus,
            include_candidates=request.include_candidates,
            include_inferred=request.include_inferred,
        )
        scope_all = self.graph.prerequisite_edges(
            request.course_id,
            syllabus_id=effective_syllabus,
            include_candidates=True,
            include_inferred=True,
            include_disabled=True,
        )
        if (
            effective_syllabus is None
            and not mixed_versions
            and self._has_mixed_versions(scope_all)
        ):
            mixed_versions = True
        if effective_syllabus is None and mixed_versions:
            plan.missing_metadata.append(
                "multiple syllabus versions exist and no syllabus was selected; "
                "only unversioned relationships were used; pass --syllabus to select one"
            )
            edges = [edge for edge in edges if edge.syllabus_id is None]
            scope_all = [edge for edge in scope_all if edge.syllabus_id is None]
        plan.config.update(
            {
                "effective_syllabus": effective_syllabus,
                "path_max_depth_effective": max_depth,
                "course_syllabus_ids": sorted(course_syllabus_ids),
                "mixed_syllabus_versions": mixed_versions,
            }
        )

        incoming: dict[str, list[GraphEdge]] = defaultdict(list)
        outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        for edge in edges:
            incoming[edge.target_node_id].append(edge)
            outgoing[edge.source_node_id].append(edge)
        for values in incoming.values():
            values.sort(key=lambda item: (item.source_node_id, item.edge_id))
        for values in outgoing.values():
            values.sort(key=lambda item: (item.target_node_id, item.edge_id))

        target_ids = {node.node_id for node in composed_targets}
        depths, unresolved = self._ancestors(target_ids, incoming, known_ids, max_depth)
        excluded_known = {
            node_id
            for node_id in known_ids
            if not request.review_known and node_id not in target_ids
        }
        known_skipped = [
            self._ref(known_nodes[node_id])
            for node_id in sorted(excluded_known, key=lambda item: (labels.get(item, item), item))
            if node_id in known_nodes and node_id in depths
        ]
        plan.known_concepts_skipped = _dedupe_refs(known_skipped)

        if request.max_path_length is not None:
            plan.limitations.append(
                f"maximum path length {request.max_path_length} may truncate required prerequisites"
            )
        if target.node_type is GraphNodeType.TOPIC:
            plan.limitations.append(
                "topic targets are composed from questions that cover the topic and the "
                "concepts those questions assess; this expansion is indirect"
            )
        if target.node_type is GraphNodeType.UNIT:
            plan.limitations.append(
                "unit targets are composed from concepts directly contained by that unit"
            )

        path_nodes = set(depths)
        path_nodes.update(node.node_id for node in composed_targets)
        relevant_edges = [
            edge
            for edge in edges
            if edge.source_node_id in path_nodes and edge.target_node_id in path_nodes
        ]
        cycles, ignored = self._break_cycles(relevant_edges, labels)
        ordered = self._topological_order(path_nodes, relevant_edges, labels, ignored, plan)
        if ordered is None:
            plan.status = LearningPathStatus.PARTIAL.value
            ordered = sorted(path_nodes, key=lambda item: (labels.get(item, item), item))
            plan.limitations.append(
                "ordering fallback was used because the dependency graph could not be resolved"
            )

        selected_edges = [edge for edge in relevant_edges if edge.edge_id not in ignored]
        plan.cycles = cycles
        plan.conflicts = find_reverse_conflicts(relevant_edges, labels)
        steps = self._steps(
            ordered,
            target_ids,
            known_ids,
            request,
            selected_edges,
            labels,
            depths,
            plan,
        )
        if request.max_path_length is not None and len(steps) > request.max_path_length:
            target_steps = [step for step in steps if step.role == PathStepRole.TARGET.value]
            prerequisite_steps = [step for step in steps if step.role != PathStepRole.TARGET.value]
            slots = max(0, request.max_path_length - len(target_steps))
            # Always retain the requested target(s). If the limit cannot fit all
            # of them, the plan exceeds the requested length but never drops the
            # destination; if there is room, keep the prerequisites nearest the
            # target rather than the most remote foundations.
            nearest = sorted(
                prerequisite_steps,
                key=lambda item: (item.depth, item.position),
            )[:slots]
            kept_ids = {id(step) for step in [*target_steps, *nearest]}
            kept = [step for step in steps if id(step) in kept_ids]
            dropped = [step for step in steps if id(step) not in kept_ids]
            for position, step in enumerate(kept, start=1):
                kept[position - 1] = step.model_copy(update={"position": position})
            plan.unresolved_concepts = _dedupe_refs(
                [
                    *plan.unresolved_concepts,
                    *[
                        self._ref_for_id(
                            step.concept_id,
                            all_concepts,
                            request.course_id,
                            effective_syllabus,
                        )
                        for step in dropped
                    ],
                ]
            )
            plan.status = LearningPathStatus.PARTIAL.value
            if len(target_steps) > request.max_path_length:
                plan.limitations.append(
                    f"maximum path length {request.max_path_length} cannot fit "
                    f"{len(target_steps)} target concept(s); all targets were retained"
                )
            else:
                plan.limitations.append(
                    f"path truncated at {request.max_path_length} step(s); "
                    f"{len(dropped)} required step(s) were omitted"
                )
            steps = kept
        plan.steps = steps
        plan.unresolved_concepts = _dedupe_refs(
            [
                *plan.unresolved_concepts,
                *[
                    self._ref_for_id(
                        node_id,
                        all_concepts,
                        request.course_id,
                        effective_syllabus,
                    )
                    for node_id in sorted(unresolved)
                ],
            ]
        )

        included_candidate = any(
            edge.status in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED)
            for edge in selected_edges
        )
        target_known = bool(target_ids & known_ids)
        if not request.review_known and target_known:
            plan.status = LearningPathStatus.OK.value
            plan.steps = []
            plan.known_concepts_skipped = _dedupe_refs(
                [
                    *plan.known_concepts_skipped,
                    *[self._ref(node) for node in composed_targets if node.node_id in known_ids],
                ]
            )
            plan.limitations.append(
                "the target concept is already marked known; no study step is recommended"
            )
            return plan
        if not selected_edges:
            if target_known and request.review_known:
                plan.status = LearningPathStatus.OK.value
                plan.limitations.append(
                    "the target concept is already known; the plan contains a review step only"
                )
            else:
                if plan.status != LearningPathStatus.PARTIAL.value:
                    plan.status = LearningPathStatus.INSUFFICIENT_EVIDENCE.value
                plan.limitations.append(
                    "no supported prerequisite relationships were found; "
                    "the plan contains only the target concept"
                )
            selected_ids = {item.edge_id for item in edges}
            excluded = [
                edge
                for edge in scope_all
                if edge.edge_id not in selected_ids
                and (edge.target_node_id in path_nodes or edge.source_node_id in path_nodes)
            ]
            excluded_policy = [
                edge
                for edge in excluded
                if edge.status in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED)
            ]
            excluded_disabled = [edge for edge in excluded if bool(edge.metadata.get("disabled"))]
            if excluded_policy:
                plan.limitations.append(
                    f"{len(excluded_policy)} candidate/inferred relationship(s) were excluded "
                    "by the default status policy"
                )
            if excluded_disabled and len(excluded_disabled) != len(excluded_policy):
                plan.limitations.append(
                    f"{len(excluded_disabled)} disabled relationship(s) were excluded from the path"
                )
        elif included_candidate:
            plan.status = LearningPathStatus.PARTIAL.value
            plan.limitations.append(
                "unverified candidate/inferred relationships were explicitly opted in; "
                "the plan is marked partial"
            )
        if plan.cycles:
            plan.status = LearningPathStatus.PARTIAL.value
            plan.limitations.append(
                f"{len(plan.cycles)} cycle(s) were broken for ordering; the underlying "
                "relationships remain in the graph for review"
            )
        if plan.conflicts:
            plan.status = LearningPathStatus.PARTIAL.value
            plan.limitations.append(
                f"{len(plan.conflicts)} conflicting reverse relationship(s) were preserved"
            )
        if plan.unresolved_concepts:
            plan.status = LearningPathStatus.PARTIAL.value
        if plan.status == LearningPathStatus.INVALID_TARGET.value:
            plan.status = LearningPathStatus.PARTIAL.value
        if plan.status == LearningPathStatus.OK.value and excluded_known:
            plan.limitations.append(
                f"{len(excluded_known)} mastered concept(s) were treated as satisfied"
            )
        return plan

    # -- targets, known state -------------------------------------------------

    def _resolve_targets(
        self,
        target: GraphNode,
        request: LearningPathRequest,
        effective_syllabus: str | None,
        mixed_versions: bool,
    ) -> tuple[list[GraphNode], str | None]:
        if target.node_type is GraphNodeType.CONCEPT:
            return [target], None
        composition_note: str | None = None
        if target.node_type is GraphNodeType.UNIT:
            unit_id = target.entity_id or target.node_key
            concepts = self.graph.concepts(
                request.course_id,
                unit_id=unit_id,
                syllabus_id=effective_syllabus,
            )
            if effective_syllabus is None and mixed_versions:
                concepts = [node for node in concepts if node.syllabus_id is None]
                composition_note = (
                    "multiple syllabus versions exist and the unit has no syllabus scope; "
                    "version-specific contained concepts were excluded"
                )
            return concepts, composition_note
        questions = self.graph.questions_for(
            request.course_id,
            topic_id=target.node_id,
            syllabus_id=effective_syllabus,
            include_candidates=request.include_candidates,
        )
        found: dict[str, GraphNode] = {}
        for question in questions:
            for concept in self.graph.concepts_for_question(
                question.question_id or question.node_id,
                course_id=request.course_id,
                syllabus_id=effective_syllabus,
                include_candidates=request.include_candidates,
            ):
                if (
                    effective_syllabus is None
                    and mixed_versions
                    and concept.syllabus_id is not None
                ):
                    continue
                found[concept.node_id] = concept
        if effective_syllabus is None and mixed_versions:
            composition_note = (
                "multiple syllabus versions exist and the topic has no syllabus scope; "
                "version-specific concept mappings were excluded"
            )
        return sorted(
            found.values(), key=lambda item: (item.label.casefold(), item.node_id)
        ), composition_note

    def _validate_known(
        self,
        request: LearningPathRequest,
        concepts: dict[str, GraphNode],
        effective_syllabus: str | None,
        plan: LearningPathPlan,
        *,
        mixed_versions: bool,
    ) -> tuple[set[str], dict[str, GraphNode]]:
        known: set[str] = set()
        nodes: dict[str, GraphNode] = {}
        for concept_id in request.known_concept_ids:
            node = concepts.get(concept_id)
            if node is None or node.node_type is not GraphNodeType.CONCEPT:
                plan.missing_metadata.append(
                    f"known concept {concept_id} is not a concept in course "
                    f"{request.course_id}; ignored"
                )
                continue
            if (
                effective_syllabus is not None
                and node.syllabus_id is not None
                and node.syllabus_id != effective_syllabus
            ):
                plan.missing_metadata.append(
                    f"known concept {concept_id} belongs to syllabus {node.syllabus_id}; ignored"
                )
                continue
            if effective_syllabus is None and mixed_versions and node.syllabus_id is not None:
                plan.missing_metadata.append(
                    f"known concept {concept_id} belongs to syllabus {node.syllabus_id}, "
                    "but no syllabus was selected; ignored"
                )
                continue
            known.add(node.node_id)
            nodes[node.node_id] = node
        return known, nodes

    def _ancestors(
        self,
        targets: set[str],
        incoming: dict[str, list[GraphEdge]],
        known_ids: set[str],
        max_depth: int,
    ) -> tuple[dict[str, int], set[str]]:
        depths: dict[str, int] = dict.fromkeys(targets, 0)
        unresolved: set[str] = set()
        queue: deque[tuple[str, int]] = deque((target, 0) for target in sorted(targets))
        while queue:
            concept_id, depth = queue.popleft()
            if concept_id in known_ids:
                continue
            for edge in incoming.get(concept_id, []):
                source = edge.source_node_id
                next_depth = depth + 1
                if source in known_ids:
                    depths.setdefault(source, next_depth)
                    continue
                if next_depth > max_depth:
                    unresolved.add(source)
                    continue
                if next_depth < depths.get(source, 10**9):
                    depths[source] = next_depth
                    queue.append((source, next_depth))
        # A node reached through a cycle can be recorded as unresolved before it
        # is known to be part of the selected path. Only nodes actually outside
        # the depth-bounded subgraph are unresolved.
        return depths, {node for node in unresolved if node not in depths}

    # -- ordering ------------------------------------------------------------

    def _break_cycles(
        self, edges: list[GraphEdge], labels: dict[str, str]
    ) -> tuple[list[PrerequisiteCycle], set[str]]:
        remaining = list(edges)
        ignored: set[str] = set()
        cycles: dict[tuple[str, ...], PrerequisiteCycle] = {}
        for _ in range(200):
            found = find_cycles(remaining, labels)
            if not found:
                break
            for cycle in found:
                key = tuple(sorted(cycle.edge_ids))
                cycles.setdefault(key, cycle)
                candidate = next(
                    (edge_id for edge_id in cycle.edge_ids if edge_id not in ignored), None
                )
                if candidate is not None:
                    ignored.add(candidate)
            remaining = [edge for edge in remaining if edge.edge_id not in ignored]
        return sorted(cycles.values(), key=lambda item: tuple(item.concept_ids)), ignored

    def _topological_order(
        self,
        nodes: set[str],
        edges: list[GraphEdge],
        labels: dict[str, str],
        ignored: set[str],
        plan: LearningPathPlan,
    ) -> list[str] | None:
        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree = dict.fromkeys(nodes, 0)
        for edge in edges:
            if edge.edge_id in ignored:
                continue
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                continue
            adjacency[edge.source_node_id].append(edge.target_node_id)
            indegree[edge.target_node_id] += 1
        for values in adjacency.values():
            values.sort(key=lambda item: (labels.get(item, item), item))
        heap = [
            (labels.get(node, node).casefold(), node)
            for node in nodes
            if indegree.get(node, 0) == 0
        ]
        heapq.heapify(heap)
        ordered: list[str] = []
        while heap:
            _, node = heapq.heappop(heap)
            ordered.append(node)
            for target in adjacency.get(node, []):
                indegree[target] -= 1
                if indegree[target] == 0:
                    heapq.heappush(heap, (labels.get(target, target).casefold(), target))
        if len(ordered) != len(nodes):
            plan.limitations.append(
                "some nodes could not be ordered after cycle breaking; deterministic fallback used"
            )
            return None
        return ordered

    # -- steps ---------------------------------------------------------------

    def _steps(
        self,
        ordered: list[str],
        target_ids: set[str],
        known_ids: set[str],
        request: LearningPathRequest,
        edges: list[GraphEdge],
        labels: dict[str, str],
        depths: dict[str, int],
        plan: LearningPathPlan,
    ) -> list[LearningPathStep]:
        incoming: dict[str, list[GraphEdge]] = defaultdict(list)
        outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        for edge in edges:
            incoming[edge.target_node_id].append(edge)
            outgoing[edge.source_node_id].append(edge)
        steps: list[LearningPathStep] = []
        for node_id in ordered:
            if node_id in known_ids and node_id not in target_ids and not request.review_known:
                continue
            role = (
                PathStepRole.TARGET.value
                if node_id in target_ids
                else (
                    PathStepRole.REVIEW.value
                    if node_id in known_ids
                    else PathStepRole.PREREQUISITE.value
                )
            )
            if role == PathStepRole.TARGET.value:
                basis = sorted(
                    incoming.get(node_id, []),
                    key=lambda item: (labels.get(item.source_node_id, ""), item.edge_id),
                )
            else:
                basis = sorted(
                    outgoing.get(node_id, []),
                    key=lambda item: (labels.get(item.target_node_id, ""), item.edge_id),
                )
            evidence: list[PrerequisiteEvidence] = []
            for edge in basis:
                evidence.extend(_evidence_from_edge(edge))
            dominant = _dominant_status(basis)
            has_unverified_basis = any(
                edge.status in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED)
                for edge in basis
            )
            dependents = sorted(
                {
                    labels.get(edge.target_node_id, edge.target_node_id)
                    for edge in outgoing.get(node_id, [])
                }
            )
            known_basis = [edge for edge in basis if edge.source_node_id in known_ids]
            if role == PathStepRole.TARGET.value:
                if basis and len(known_basis) == len(basis):
                    rationale = (
                        f"Target concept; {len(basis)} prerequisite relationship(s) are "
                        "already marked known, so no study step is required for them."
                    )
                elif basis:
                    rationale = (
                        f"Target concept; {len(basis) - len(known_basis)} unresolved "
                        "prerequisite relationship(s) were ordered before it."
                    )
                else:
                    rationale = "Target concept; no supported prerequisite relationship was found."
            elif role == PathStepRole.REVIEW.value:
                rationale = (
                    "Marked known by the learner; included only for review because "
                    "review_known was requested."
                )
            elif basis:
                rationale = (
                    f"Required before {', '.join(repr(item) for item in dependents)}; "
                    f"basis: {_basis_text(basis)}."
                )
            else:
                rationale = (
                    "Ancestor of the target; its supporting relationship was removed only "
                    "while breaking a cycle, so treat this position as unresolved."
                )
            if dominant in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED):
                rationale += (
                    " This is an unverified candidate relationship included only because "
                    "the request opted in."
                )
            elif has_unverified_basis:
                rationale += (
                    " This step also depends on at least one unverified candidate/inferred "
                    "relationship; review is recommended."
                )
            if has_unverified_basis:
                plan.limitations.append(
                    f"step '{labels.get(node_id, node_id)}' uses at least one unverified "
                    "candidate/inferred relationship"
                )
            direct = role == PathStepRole.PREREQUISITE.value and any(
                edge.target_node_id in target_ids for edge in outgoing.get(node_id, [])
            )
            step = LearningPathStep(
                position=len(steps) + 1,
                concept_id=node_id,
                label=labels.get(node_id, node_id),
                role=role,
                depth=depths.get(node_id, 0),
                direct=direct,
                status=dominant,
                confidence=basis[0].confidence if len(basis) == 1 else None,
                method="+".join(sorted({edge.method for edge in basis})) or None,
                edge_ids=[edge.edge_id for edge in basis],
                evidence=_dedupe_evidence(evidence),
                already_known=node_id in known_ids,
                review_recommended=has_unverified_basis,
                rationale=rationale,
            )
            steps.append(step)
        return steps

    @staticmethod
    def _has_mixed_versions(edges: list[GraphEdge]) -> bool:
        return len({edge.syllabus_id for edge in edges if edge.syllabus_id}) > 1

    @staticmethod
    def _ref(node: GraphNode) -> ConceptRef:
        return make_concept_ref(node)

    @staticmethod
    def _ref_for_id(
        node_id: str,
        concepts: dict[str, GraphNode],
        course_id: str,
        syllabus_id: str | None,
    ) -> ConceptRef:
        node = concepts.get(node_id)
        if node is not None:
            return make_concept_ref(node)
        return ConceptRef(
            concept_id=node_id,
            label=node_id,
            course_id=course_id,
            syllabus_id=syllabus_id,
        )


def _dominant_status(edges: list[GraphEdge]) -> GraphEdgeStatus | None:
    if not edges:
        return None
    return max((edge.status for edge in edges), key=lambda item: _STATUS_PRIORITY[item])


def _basis_text(edges: list[GraphEdge]) -> str:
    if not edges:
        return "no selected relationship"
    parts = sorted(
        {
            f"{edge.method} ({edge.status.value})"
            if edge.status.value != "explicit"
            else edge.method
            for edge in edges
        }
    )
    return ", ".join(parts)


def _dedupe_evidence(items: list[PrerequisiteEvidence]) -> list[PrerequisiteEvidence]:
    seen: set[tuple[str, str, str, str | None, int | None]] = set()
    result: list[PrerequisiteEvidence] = []
    for item in sorted(
        items,
        key=lambda value: (
            value.source,
            value.method,
            value.detail,
            value.source_document_id or "",
            value.source_page or 0,
        ),
    ):
        key = (
            item.source,
            item.method,
            item.detail,
            item.source_document_id,
            item.source_page,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_refs(items: list[ConceptRef]) -> list[ConceptRef]:
    seen: set[str] = set()
    result: list[ConceptRef] = []
    for item in items:
        if item.concept_id in seen:
            continue
        seen.add(item.concept_id)
        result.append(item)
    return result


__all__ = ["ALGORITHM", "ALGORITHM_VERSION", "LearningPathService"]
