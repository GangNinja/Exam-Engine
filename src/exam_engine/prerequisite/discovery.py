"""Deterministic prerequisite candidate discovery over the Phase 5 graph.

Discovery only creates *candidates*. Directional evidence comes from syllabus
order (weak) or an existing explicit/reviewed edge; co-occurrence and semantic
relatedness are supporting evidence and never invent a direction on their own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from exam_engine.config import PrerequisiteSettings
from exam_engine.graph.service import GraphService
from exam_engine.models.enums import GraphEdgeStatus, GraphRelationship
from exam_engine.models.graph import GraphEdge, GraphNode, make_edge_id
from exam_engine.models.prerequisite import (
    ConceptRef,
    EvidenceSource,
    PrerequisiteConflict,
    PrerequisiteCycle,
    PrerequisiteDiscoveryReport,
    PrerequisiteDiscoveryRequest,
    PrerequisiteEvidence,
    PrerequisiteRelationship,
    PrerequisiteValidationReport,
)
from exam_engine.models.syllabus import SyllabusUnitRecord
from exam_engine.nlp.keywords import normalize_term
from exam_engine.prerequisite.graph_ops import (
    concept_ref as make_concept_ref,
)
from exam_engine.prerequisite.graph_ops import (
    find_cycles,
    find_reverse_conflicts,
)
from exam_engine.storage.graph_repository import GraphRepository
from exam_engine.storage.repositories import CourseRepository, SyllabusRepository

ALGORITHM = "phase6_prerequisite_discovery"
VALIDATION_ALGORITHM = "phase6_prerequisite_validation"
ALGORITHM_VERSION = "phase6.v1"

_ORDER_LIMITATION = "Syllabus ordering is weak evidence; it does not prove a dependency."
_QUESTION_LIMITATION = (
    "Question association depends on Phase 3 mappings; it never creates direction."
)
_COOCCURRENCE_LIMITATION = (
    "Repeated co-occurrence shows association only; direction still comes from syllabus order."
)
_RELATED_LIMITATION = (
    "Semantic relatedness supports association only; it does not establish prerequisite direction."
)


@dataclass
class _Proposal:
    source_id: str
    target_id: str
    evidence: list[PrerequisiteEvidence] = field(default_factory=list)
    methods: set[str] = field(default_factory=set)
    syllabus_ids: set[str] = field(default_factory=set)
    unit_ids: set[str] = field(default_factory=set)
    document_ids: set[str] = field(default_factory=set)
    pages: set[int] = field(default_factory=set)
    distances: set[int] = field(default_factory=set)
    limitations: set[str] = field(default_factory=set)
    conflict_with: set[str] = field(default_factory=set)

    @property
    def edge_id(self) -> str:
        return make_edge_id(self.source_id, GraphRelationship.PREREQUISITE_OF, self.target_id)


class PrerequisiteDiscoveryService:
    """Discover and persist reviewable prerequisite candidates for one scope."""

    def __init__(self, graph: GraphService, settings: PrerequisiteSettings | None = None) -> None:
        self.graph = graph
        self.settings = settings or PrerequisiteSettings()

    def _course_syllabus_ids(self, course_id: str) -> set[str]:
        """Return syllabus scopes known from records or the stored graph.

        A record may be absent in a partially rebuilt database while its graph
        nodes and edges still exist. Accepting those scopes keeps queries useful;
        a scope absent from both is an explicit error rather than an empty result.
        """
        with self.graph.db.session() as session:
            if CourseRepository(session).get(course_id) is None:
                raise KeyError(f"unknown course: {course_id}")
            records = SyllabusRepository(session).get_by_course(course_id)
        scopes = {item.syllabus_id for item in records}
        scopes.update(
            node.syllabus_id
            for node in self.graph.concepts(course_id)
            if node.syllabus_id is not None
        )
        scopes.update(
            edge.syllabus_id
            for edge in self.graph.prerequisite_edges(
                course_id,
                include_candidates=True,
                include_inferred=True,
                include_disabled=True,
            )
            if edge.syllabus_id is not None
        )
        return scopes

    def discover(self, request: PrerequisiteDiscoveryRequest) -> PrerequisiteDiscoveryReport:
        syllabus_ids = self._course_syllabus_ids(request.course_id)
        if request.syllabus_id is not None and request.syllabus_id not in syllabus_ids:
            raise KeyError(f"unknown syllabus {request.syllabus_id} for course {request.course_id}")

        concepts = self.graph.concepts(request.course_id, syllabus_id=request.syllabus_id)
        all_concepts = self.graph.concepts(request.course_id)
        labels = {node.node_id: node.label for node in all_concepts}
        concepts_by_node = {node.node_id: node for node in all_concepts}
        existing = self.graph.prerequisite_edges(
            request.course_id,
            syllabus_id=request.syllabus_id,
            include_candidates=True,
            include_inferred=True,
            include_disabled=True,
        )
        report = PrerequisiteDiscoveryReport(
            course_id=request.course_id,
            syllabus_id=request.syllabus_id,
            algorithm=ALGORITHM,
            algorithm_version=ALGORITHM_VERSION,
            config={**self.settings.as_config(), **request.model_dump(mode="json")},
            nodes_scanned=len(concepts),
            edges_existing=len(existing),
            limitations=[_ORDER_LIMITATION] if request.include_order_evidence else [],
        )
        report.existing_relationships = [
            self._relationship_from_edge(edge, concepts_by_node) for edge in existing
        ]
        if not concepts:
            report.limitations.append(
                "no concepts were available in this scope; build the Phase 5 graph first"
            )
            return report

        proposals = self._order_proposals(request, concepts, labels)
        if request.include_question_evidence:
            omitted_cooccurrence = self._add_question_evidence(request, proposals, concepts_by_node)
            if omitted_cooccurrence:
                report.limitations.append(
                    f"{omitted_cooccurrence} candidate pair(s) exceeded "
                    "max_cooccurrence_candidates and kept question-association evidence "
                    "without the additional co-occurrence signal"
                )
        self._add_related_evidence(request, proposals)

        existing_by_pair = {(edge.source_node_id, edge.target_node_id): edge for edge in existing}
        existing_by_id = {edge.edge_id: edge for edge in existing}
        capped, dropped = self._apply_cap(proposals, concepts_by_node)
        if dropped:
            report.limitations.append(
                f"{dropped} candidate pair(s) were omitted by max_candidates_per_concept; "
                "raise EXAM_ENGINE_Q6_MAX_CANDIDATES to inspect them"
            )

        effective_edges: list[GraphEdge] = list(existing)
        for proposal in capped:
            reverse = existing_by_pair.get((proposal.target_id, proposal.source_id))
            same = existing_by_pair.get((proposal.source_id, proposal.target_id))
            if (
                reverse is not None
                and reverse.status
                in (
                    GraphEdgeStatus.EXPLICIT,
                    GraphEdgeStatus.REVIEWED,
                    GraphEdgeStatus.VERIFIED,
                )
                and (
                    same is None
                    or same.status
                    not in (
                        GraphEdgeStatus.EXPLICIT,
                        GraphEdgeStatus.REVIEWED,
                        GraphEdgeStatus.VERIFIED,
                    )
                )
            ):
                proposal.conflict_with.add(reverse.edge_id)
                proposal.limitations.add(
                    "a supported relationship exists in the opposite direction; "
                    "this candidate is disabled pending review"
                )
            edge = self._edge_for(proposal, request, concepts_by_node)
            if request.persist:
                created = self.graph.upsert_edge(edge)
                if created:
                    report.edges_created += 1
                else:
                    report.edges_updated += 1
            effective_edges = [item for item in effective_edges if item.edge_id != edge.edge_id]
            stored = self.graph.edge(edge.edge_id) if request.persist else None
            effective = stored or self._merge_preview(edge, existing_by_id.get(edge.edge_id))
            effective_edges.append(effective)
            report.candidates.append(
                self._relationship_from_edge(
                    effective,
                    concepts_by_node,
                    extra_evidence=proposal.evidence,
                    in_cycle=False,
                )
            )

        cycles = _find_cycles(effective_edges, labels)
        cycle_edges = {edge_id for cycle in cycles for edge_id in cycle.edge_ids}
        for candidate in report.candidates:
            candidate.in_cycle = candidate.edge_id in cycle_edges
        for relationship in report.existing_relationships:
            relationship.in_cycle = relationship.edge_id in cycle_edges
        report.cycles = cycles
        report.conflicts = _find_reverse_conflicts(effective_edges, labels)
        for conflict in report.conflicts:
            if any(edge_id in cycle_edges for edge_id in conflict.edge_ids):
                conflict.blocking = True
        touched = {
            concept_id
            for edge in effective_edges
            for concept_id in (edge.source_node_id, edge.target_node_id)
        }
        report.isolated_concepts = [
            _concept_ref(node, labels) for node in concepts if node.node_id not in touched
        ]
        if cycles:
            report.limitations.append(
                f"{len(cycles)} prerequisite cycle(s) were preserved; learning paths "
                "must break them explicitly rather than looping"
            )
        return report

    def validate(
        self,
        course_id: str,
        *,
        syllabus_id: str | None = None,
        include_candidates: bool = True,
        include_inferred: bool = True,
    ) -> PrerequisiteValidationReport:
        syllabus_ids = self._course_syllabus_ids(course_id)
        if syllabus_id is not None and syllabus_id not in syllabus_ids:
            raise KeyError(f"unknown syllabus {syllabus_id} for course {course_id}")
        edges = self.graph.prerequisite_edges(
            course_id,
            syllabus_id=syllabus_id,
            include_candidates=include_candidates,
            include_inferred=include_inferred,
            include_disabled=True,
        )
        concepts = self.graph.concepts(course_id, syllabus_id=syllabus_id)
        labels = {node.node_id: node.label for node in self.graph.concepts(course_id)}
        report = PrerequisiteValidationReport(
            course_id=course_id,
            syllabus_id=syllabus_id,
            algorithm=VALIDATION_ALGORITHM,
            algorithm_version=ALGORITHM_VERSION,
            config={
                **self.settings.as_config(),
                "include_candidates": include_candidates,
                "include_inferred": include_inferred,
            },
            relationship_count=len(edges),
            candidate_count=sum(
                1
                for edge in edges
                if edge.status in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED)
            ),
            supported_count=sum(
                1
                for edge in edges
                if edge.status
                in (
                    GraphEdgeStatus.EXPLICIT,
                    GraphEdgeStatus.REVIEWED,
                    GraphEdgeStatus.VERIFIED,
                )
            ),
            limitations=[
                "Cycle detection reports but never deletes relationships.",
                "Reverse-direction pairs are conflicts, not automatically resolved facts.",
            ],
        )
        report.cycles = _find_cycles(edges, labels)
        report.conflicts = _find_reverse_conflicts(edges, labels)
        touched = {
            concept_id
            for edge in edges
            for concept_id in (edge.source_node_id, edge.target_node_id)
        }
        report.isolated_concepts = [
            _concept_ref(node, labels) for node in concepts if node.node_id not in touched
        ]
        return report

    # -- evidence ----------------------------------------------------------

    def _order_proposals(
        self,
        request: PrerequisiteDiscoveryRequest,
        concepts: list[GraphNode],
        labels: dict[str, str],
    ) -> list[_Proposal]:
        if not request.include_order_evidence:
            return []
        with self.graph.db.session() as session:
            syllabi = SyllabusRepository(session).get_by_course(request.course_id)
        if request.syllabus_id is not None:
            syllabi = [item for item in syllabi if item.syllabus_id == request.syllabus_id]
        by_label: dict[str, list[GraphNode]] = defaultdict(list)
        for graph_node in sorted(concepts, key=lambda item: (item.label.casefold(), item.node_id)):
            key = graph_node.normalized_label or normalize_term(graph_node.label)
            by_label[key].append(graph_node)
        proposals: dict[str, _Proposal] = {}

        for syllabus in sorted(syllabi, key=lambda item: item.syllabus_id):
            units = sorted(
                syllabus.units,
                key=lambda item: (
                    item.unit_number if item.unit_number is not None else 10**9,
                    item.unit_id,
                ),
            )
            ordered_per_unit: list[tuple[SyllabusUnitRecord, list[GraphNode]]] = []
            for unit in units:
                ordered: list[GraphNode] = []
                for topic in unit.topics:
                    matches = by_label.get(normalize_term(topic), [])
                    matched: GraphNode | None = next(
                        (item for item in matches if item.syllabus_id == syllabus.syllabus_id),
                        None,
                    )
                    if matched is None:
                        matched = next(
                            (item for item in matches if item.syllabus_id is None),
                            None,
                        )
                    if matched is not None and matched not in ordered:
                        ordered.append(matched)
                ordered_per_unit.append((unit, ordered))
                for source_index, source in enumerate(ordered):
                    for distance in range(1, self.settings.order_window + 1):
                        target_index = source_index + distance
                        if target_index >= len(ordered):
                            break
                        target = ordered[target_index]
                        proposal = proposals.setdefault(
                            f"{source.node_id}\x1f{target.node_id}",
                            _Proposal(source.node_id, target.node_id),
                        )
                        proposal.methods.add("syllabus_topic_order")
                        proposal.syllabus_ids.add(syllabus.syllabus_id)
                        proposal.unit_ids.add(unit.unit_id)
                        proposal.document_ids.add(syllabus.document_id)
                        if unit.page_number is not None:
                            proposal.pages.add(unit.page_number)
                        proposal.distances.add(distance)
                        proposal.limitations.add(_ORDER_LIMITATION)
                        proposal.evidence.append(
                            PrerequisiteEvidence(
                                source=EvidenceSource.SYLLABUS_ORDER.value,
                                method="syllabus_topic_order",
                                detail=(
                                    f"'{labels.get(source.node_id, source.label)}' appears "
                                    f"{distance} topic position(s) before "
                                    f"'{labels.get(target.node_id, target.label)}' in "
                                    f"{syllabus.syllabus_id}/{unit.unit_id}"
                                ),
                                source_document_id=syllabus.document_id,
                                source_page=unit.page_number,
                                method_version=ALGORITHM_VERSION,
                                confidence=None,
                                limitations=[_ORDER_LIMITATION],
                                provenance={
                                    "syllabus_id": syllabus.syllabus_id,
                                    "unit_id": unit.unit_id,
                                    "source_position": source_index,
                                    "target_position": target_index,
                                    "distance": distance,
                                },
                            )
                        )

            if self.settings.include_cross_unit_order:
                for (left_unit, left), (right_unit, right) in zip(
                    ordered_per_unit, ordered_per_unit[1:], strict=False
                ):
                    if not left or not right:
                        continue
                    source, target = left[-1], right[0]
                    if source.node_id == target.node_id:
                        # The same concept can close one unit and open the next;
                        # a self-edge is not a prerequisite candidate.
                        continue
                    proposal = proposals.setdefault(
                        f"{source.node_id}\x1f{target.node_id}",
                        _Proposal(source.node_id, target.node_id),
                    )
                    proposal.methods.add("syllabus_unit_order")
                    proposal.syllabus_ids.add(syllabus.syllabus_id)
                    proposal.unit_ids.update({left_unit.unit_id, right_unit.unit_id})
                    proposal.document_ids.add(syllabus.document_id)
                    for page in (left_unit.page_number, right_unit.page_number):
                        if page is not None:
                            proposal.pages.add(page)
                    proposal.limitations.add(
                        "Cross-unit ordering is especially weak; units may be taught "
                        "in another order."
                    )
                    proposal.evidence.append(
                        PrerequisiteEvidence(
                            source=EvidenceSource.SYLLABUS_CONTAINMENT.value,
                            method="syllabus_unit_order",
                            detail=(
                                f"'{labels.get(source.node_id, source.label)}' ends "
                                f"{left_unit.unit_id} and "
                                f"'{labels.get(target.node_id, target.label)}' starts "
                                f"{right_unit.unit_id}"
                            ),
                            source_document_id=syllabus.document_id,
                            method_version=ALGORITHM_VERSION,
                            confidence=None,
                            limitations=[
                                "Cross-unit ordering is weak evidence, not proof of dependency."
                            ],
                            provenance={
                                "syllabus_id": syllabus.syllabus_id,
                                "source_unit_id": left_unit.unit_id,
                                "target_unit_id": right_unit.unit_id,
                                "source_position": len(left) - 1,
                                "target_position": 0,
                            },
                        )
                    )
        return sorted(proposals.values(), key=lambda item: (item.source_id, item.target_id))

    def _add_question_evidence(
        self,
        request: PrerequisiteDiscoveryRequest,
        proposals: list[_Proposal],
        concepts: dict[str, GraphNode],
    ) -> int:
        """Attach question evidence; return the number of capped co-occurrence pairs.

        Question-association evidence is attached to every ordered pair. The
        optional repeated-co-occurrence signal is capped deterministically by
        ``max_cooccurrence_candidates`` so a large question bank cannot create
        unbounded evidence payloads.
        """
        if not proposals:
            return 0
        with self.graph.db.session() as session:
            repo = GraphRepository(session)
            assessments = repo.list_edges(
                course_id=request.course_id,
                syllabus_id=request.syllabus_id,
                relationship=GraphRelationship.ASSESSES,
            )
        questions_by_concept: dict[str, set[str]] = defaultdict(set)
        for edge in assessments:
            if edge.target_node_id not in concepts:
                continue
            questions_by_concept[edge.target_node_id].add(edge.source_node_id)
        if not questions_by_concept:
            return 0

        eligible: list[tuple[int, float, _Proposal, set[str], set[str], set[str]]] = []
        for proposal in proposals:
            left = questions_by_concept.get(proposal.source_id, set())
            right = questions_by_concept.get(proposal.target_id, set())
            shared = left & right
            if not shared:
                continue
            proposal.methods.add("question_association")
            example_ids = sorted(shared)[:5]
            proposal.evidence.append(
                PrerequisiteEvidence(
                    source=EvidenceSource.QUESTION_CONCEPT.value,
                    method="question_association",
                    detail=(
                        f"{len(shared)} question(s) assess both concepts; "
                        f"examples: {', '.join(example_ids)}"
                    ),
                    method_version=ALGORITHM_VERSION,
                    confidence=None,
                    limitations=[_QUESTION_LIMITATION],
                    provenance={"question_ids": sorted(shared)},
                )
            )
            if not request.include_cooccurrence:
                continue
            union = left | right
            jaccard = len(shared) / len(union) if union else 0.0
            if (
                len(shared) >= self.settings.cooccurrence_min_questions
                and jaccard >= self.settings.cooccurrence_min_jaccard
            ):
                eligible.append((len(shared), jaccard, proposal, left, right, shared))

        eligible.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                item[2].source_id,
                item[2].target_id,
            )
        )
        kept = eligible[: request.max_cooccurrence_candidates]
        for shared_count, jaccard, proposal, left, right, shared in kept:
            proposal.methods.add("question_cooccurrence")
            proposal.evidence.append(
                PrerequisiteEvidence(
                    source=EvidenceSource.COOCCURRENCE.value,
                    method="question_cooccurrence",
                    detail=(f"shared question support={shared_count}, jaccard={jaccard:.3f}"),
                    method_version=ALGORITHM_VERSION,
                    confidence=None,
                    limitations=[_COOCCURRENCE_LIMITATION],
                    provenance={
                        "shared_questions": shared_count,
                        "left_questions": len(left),
                        "right_questions": len(right),
                        "jaccard": round(jaccard, 6),
                        "question_ids": sorted(shared),
                    },
                )
            )
        return len(eligible) - len(kept)

    def _add_related_evidence(
        self, request: PrerequisiteDiscoveryRequest, proposals: list[_Proposal]
    ) -> None:
        if not proposals:
            return
        with self.graph.db.session() as session:
            related = GraphRepository(session).list_edges(
                course_id=request.course_id,
                relationship=GraphRelationship.RELATED_TO,
            )
        index: dict[tuple[str, str], GraphEdge] = {}
        for edge in related:
            if request.syllabus_id is not None and edge.syllabus_id not in (
                None,
                request.syllabus_id,
            ):
                continue
            index[(edge.source_node_id, edge.target_node_id)] = edge
            index[(edge.target_node_id, edge.source_node_id)] = edge
        for proposal in proposals:
            related_edge = index.get((proposal.source_id, proposal.target_id))
            if related_edge is None:
                continue
            proposal.methods.add("related_concept")
            proposal.evidence.append(
                PrerequisiteEvidence(
                    source=EvidenceSource.RELATED_CONCEPT.value,
                    method=related_edge.method,
                    detail=f"existing RELATED_TO edge {related_edge.edge_id}",
                    source_document_id=related_edge.source_document_id,
                    source_page=related_edge.source_page,
                    method_version=related_edge.model_version,
                    confidence=related_edge.confidence,
                    limitations=[_RELATED_LIMITATION],
                    provenance={
                        "edge_id": related_edge.edge_id,
                        "status": related_edge.status.value,
                    },
                )
            )

    # -- persistence -------------------------------------------------------

    def _apply_cap(
        self, proposals: list[_Proposal], concepts: dict[str, GraphNode]
    ) -> tuple[list[_Proposal], int]:
        cap = self.settings.max_candidates_per_concept
        by_source: dict[str, list[_Proposal]] = defaultdict(list)
        for proposal in proposals:
            by_source[proposal.source_id].append(proposal)
        kept: list[_Proposal] = []
        dropped = 0

        def _item_key(item: _Proposal) -> tuple[int, str, str]:
            target_node = concepts.get(item.target_id)
            return (
                min(item.distances) if item.distances else 10**9,
                target_node.label.casefold() if target_node else item.target_id,
                item.target_id,
            )

        for source in sorted(by_source):
            items = sorted(by_source[source], key=_item_key)
            kept.extend(items[:cap])
            dropped += max(0, len(items) - cap)
        return sorted(kept, key=lambda item: (item.source_id, item.target_id)), dropped

    def _edge_for(
        self,
        proposal: _Proposal,
        request: PrerequisiteDiscoveryRequest,
        concepts: dict[str, GraphNode],
    ) -> GraphEdge:
        evidence_payload = _dedupe_evidence(proposal.evidence)
        documents = sorted(proposal.document_ids)
        pages = sorted(proposal.pages)
        syllabus_ids = sorted(proposal.syllabus_ids)
        return GraphEdge(
            edge_id=proposal.edge_id,
            course_id=request.course_id,
            syllabus_id=syllabus_ids[0] if len(syllabus_ids) == 1 else None,
            source_node_id=proposal.source_id,
            target_node_id=proposal.target_id,
            relationship=GraphRelationship.PREREQUISITE_OF,
            status=GraphEdgeStatus.CANDIDATE,
            confidence=None,
            method="+".join(sorted(proposal.methods)),
            evidence=sorted(
                {
                    f"{item.source}:{item.detail}" if item.source else item.detail
                    for item in evidence_payload
                }
            ),
            source_document_id=documents[0] if len(documents) == 1 else None,
            source_page=pages[0] if len(pages) == 1 else None,
            model_version=ALGORITHM_VERSION,
            metadata={
                "origin": "phase6_discovery",
                "algorithm": ALGORITHM,
                "algorithm_version": ALGORITHM_VERSION,
                "validated": False,
                "review_required": True,
                "evidence_sources": sorted({item.source for item in evidence_payload}),
                "evidence_methods": sorted(proposal.methods),
                "syllabus_ids": syllabus_ids,
                "unit_ids": sorted(proposal.unit_ids),
                "document_ids": documents,
                "distances": sorted(proposal.distances),
                "limitations": sorted(proposal.limitations),
                "prerequisite_evidence": [
                    item.model_dump(mode="json") for item in evidence_payload
                ],
                "configuration": {
                    "order_window": self.settings.order_window,
                    "include_cross_unit_order": self.settings.include_cross_unit_order,
                    "cooccurrence_min_questions": self.settings.cooccurrence_min_questions,
                    "cooccurrence_min_jaccard": self.settings.cooccurrence_min_jaccard,
                    "max_cooccurrence_candidates": request.max_cooccurrence_candidates,
                    "include_order_evidence": request.include_order_evidence,
                    "include_question_evidence": request.include_question_evidence,
                    "include_cooccurrence": request.include_cooccurrence,
                },
                "disabled": bool(proposal.conflict_with),
                "conflict_with": sorted(proposal.conflict_with),
                "search_version": ALGORITHM_VERSION,
            },
        )

    def _merge_preview(self, edge: GraphEdge, existing: GraphEdge | None) -> GraphEdge:
        if existing is None:
            return edge
        merged = edge.model_copy(deep=True)
        merged.created_at = existing.created_at
        merged.evidence = sorted({*existing.evidence, *merged.evidence})
        merged.method = "+".join(sorted({*existing.method.split("+"), *merged.method.split("+")}))
        merged.metadata = _merge_metadata(existing.metadata, merged.metadata)
        priority = {
            GraphEdgeStatus.CANDIDATE: 0,
            GraphEdgeStatus.INFERRED: 1,
            GraphEdgeStatus.EXPLICIT: 2,
            GraphEdgeStatus.REVIEWED: 3,
            GraphEdgeStatus.VERIFIED: 4,
        }
        if priority[existing.status] > priority[merged.status]:
            merged.status = existing.status
        if merged.status in (
            GraphEdgeStatus.EXPLICIT,
            GraphEdgeStatus.REVIEWED,
            GraphEdgeStatus.VERIFIED,
        ):
            merged.metadata["validated"] = True
            merged.metadata["review_required"] = False
        else:
            merged.metadata["validated"] = False
            merged.metadata["review_required"] = True
        override = existing.metadata.get("manual_override")
        if override == "disabled" or (override is None and existing.metadata.get("disabled")):
            merged.metadata["disabled"] = True
        elif override == "enabled":
            merged.metadata["disabled"] = False
        merged.confidence = existing.confidence
        return merged

    # -- conversion --------------------------------------------------------

    def _relationship_from_edge(
        self,
        edge: GraphEdge,
        concepts: dict[str, GraphNode],
        *,
        extra_evidence: list[PrerequisiteEvidence] | None = None,
        in_cycle: bool = False,
    ) -> PrerequisiteRelationship:
        evidence = _evidence_from_edge(edge)
        if extra_evidence:
            evidence = _dedupe_evidence([*evidence, *extra_evidence])
        raw_limitations = edge.metadata.get("limitations")
        if isinstance(raw_limitations, str):
            limitations = [raw_limitations]
        elif isinstance(raw_limitations, list):
            limitations = [str(item) for item in raw_limitations]
        else:
            limitations = []
        return PrerequisiteRelationship(
            edge_id=edge.edge_id,
            source=_concept_ref_for_id(edge.source_node_id, concepts),
            target=_concept_ref_for_id(edge.target_node_id, concepts),
            status=edge.status,
            method=edge.method,
            confidence=edge.confidence,
            evidence=evidence,
            source_document_id=edge.source_document_id,
            source_page=edge.source_page,
            model_version=edge.model_version,
            limitations=sorted(
                {
                    *limitations,
                    *(
                        []
                        if edge.status
                        in (
                            GraphEdgeStatus.EXPLICIT,
                            GraphEdgeStatus.REVIEWED,
                            GraphEdgeStatus.VERIFIED,
                        )
                        else [_ORDER_LIMITATION]
                    ),
                }
            ),
            in_cycle=in_cycle,
            review_required=edge.status in (GraphEdgeStatus.CANDIDATE, GraphEdgeStatus.INFERRED),
        )


def _evidence_from_edge(edge: GraphEdge) -> list[PrerequisiteEvidence]:
    fallback_source = (
        EvidenceSource.SYLLABUS_EXPLICIT.value
        if edge.status is GraphEdgeStatus.EXPLICIT
        and edge.relationship is GraphRelationship.PREREQUISITE_OF
        else EvidenceSource.PREEXISTING.value
    )
    raw = edge.metadata.get("prerequisite_evidence")
    evidence: list[PrerequisiteEvidence] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                evidence.append(PrerequisiteEvidence.model_validate(item))
            except Exception:  # noqa: BLE001 - old metadata stays readable as strings
                continue
    if evidence:
        # A candidate merged into an explicit edge keeps its supporting evidence.
        # The explicit fact itself still needs its own distinguishable source.
        if fallback_source == EvidenceSource.SYLLABUS_EXPLICIT.value and not any(
            item.source == EvidenceSource.SYLLABUS_EXPLICIT.value for item in evidence
        ):
            evidence.append(
                PrerequisiteEvidence(
                    source=EvidenceSource.SYLLABUS_EXPLICIT.value,
                    method=edge.method,
                    detail=(
                        f"explicit prerequisite edge {edge.edge_id} "
                        f"(source={edge.metadata.get('source', 'graph status')})"
                    ),
                    source_document_id=edge.source_document_id,
                    source_page=edge.source_page,
                    method_version=edge.model_version,
                    confidence=None,
                    provenance={"edge_id": edge.edge_id, "status": edge.status.value},
                )
            )
        return _dedupe_evidence(evidence)
    if not edge.evidence:
        return [
            PrerequisiteEvidence(
                source=fallback_source,
                method=edge.method,
                detail=f"existing {edge.relationship.value} edge {edge.edge_id}",
                source_document_id=edge.source_document_id,
                source_page=edge.source_page,
                method_version=edge.model_version,
                confidence=edge.confidence,
                provenance={"edge_id": edge.edge_id, "status": edge.status.value},
            )
        ]
    return _dedupe_evidence(
        [
            PrerequisiteEvidence(
                source=fallback_source,
                method=edge.method,
                detail=item,
                source_document_id=edge.source_document_id,
                source_page=edge.source_page,
                method_version=edge.model_version,
                confidence=edge.confidence,
                provenance={"edge_id": edge.edge_id, "status": edge.status.value},
            )
            for item in edge.evidence
        ]
    )


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


def _merge_metadata(base: dict[str, object], extra: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            combined = [*current]
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_metadata(current, value)
        else:
            merged[key] = value
    return merged


def _concept_ref(node: GraphNode, labels: dict[str, str]) -> ConceptRef:
    reference = make_concept_ref(node)
    if node.node_id in labels:
        reference.label = labels[node.node_id]
    return reference


def _concept_ref_for_id(concept_id: str, concepts: dict[str, GraphNode]) -> ConceptRef:
    node = concepts.get(concept_id)
    if node is None:
        return ConceptRef(concept_id=concept_id, label=concept_id)
    return make_concept_ref(node)


def _find_cycles(edges: list[GraphEdge], labels: dict[str, str]) -> list[PrerequisiteCycle]:
    return find_cycles(edges, labels)


def _find_reverse_conflicts(
    edges: list[GraphEdge], labels: dict[str, str]
) -> list[PrerequisiteConflict]:
    return find_reverse_conflicts(edges, labels)


__all__ = [
    "ALGORITHM",
    "ALGORITHM_VERSION",
    "VALIDATION_ALGORITHM",
    "PrerequisiteDiscoveryService",
]
