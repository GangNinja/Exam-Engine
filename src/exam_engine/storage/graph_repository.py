"""SQLAlchemy repository for the Phase 5 knowledge graph.

The graph is intentionally stored in the existing SQLite database as two
indexed adjacency tables. The repository hides SQL from the service and query
layer, so a different backend can replace it without changing graph semantics.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from sqlalchemy import delete, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from exam_engine.models.course import utcnow
from exam_engine.models.enums import GraphEdgeStatus, GraphNodeType, GraphRelationship
from exam_engine.models.graph import GraphEdge, GraphNode
from exam_engine.storage import graph_mappers as mappers
from exam_engine.storage.mappers import dumps, iso, loads, parse_dt
from exam_engine.storage.schema import GraphEdgeRow, GraphNodeRow

Direction = Literal["in", "out", "both"]


class GraphRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- writes ------------------------------------------------------------

    def replace_course(
        self, course_id: str, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]
    ) -> int:
        """Replace one course's graph atomically, preserving shared nodes.

        Institution and subject nodes may be shared by several courses; nodes
        without a ``course_id`` are never deleted by a course rebuild. Every
        edge carries the course it was derived for, so stale edges cannot
        survive a rebuild.
        """
        node_list = list(nodes)
        edge_list = list(edges)
        previous_nodes = {
            row.node_id: row
            for row in self.session.scalars(
                select(GraphNodeRow).where(GraphNodeRow.course_id == course_id)
            ).all()
        }
        previous_edges = {
            row.edge_id: row
            for row in self.session.scalars(
                select(GraphEdgeRow).where(GraphEdgeRow.course_id == course_id)
            ).all()
        }
        self.session.execute(delete(GraphEdgeRow).where(GraphEdgeRow.course_id == course_id))
        self.session.execute(delete(GraphNodeRow).where(GraphNodeRow.course_id == course_id))
        self.session.flush()

        for node in node_list:
            node_previous = previous_nodes.get(node.node_id) or self.session.get(
                GraphNodeRow, node.node_id
            )
            if node_previous is not None and node_previous.created_at:
                node.created_at = parse_dt(node_previous.created_at) or node.created_at
            if node.course_id is None and node_previous is not None:
                previous_meta = loads(node_previous.metadata_json, {})
                previous_titles = (
                    previous_meta.get("course_titles") if isinstance(previous_meta, dict) else None
                )
                if isinstance(previous_titles, list):
                    titles = {
                        *[str(item) for item in previous_titles],
                        *[str(item) for item in node.metadata.get("course_titles", [])],
                    }
                    node.metadata["course_titles"] = sorted(titles)
            self.session.merge(mappers.node_to_row(node))
        self.session.flush()

        node_ids = {node.node_id for node in node_list}
        missing = {
            edge.edge_id
            for edge in edge_list
            if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids
        }
        if missing:
            raise ValueError(
                "graph edge(s) reference nodes outside the built graph: "
                + ", ".join(sorted(missing)[:5])
            )
        new_edge_ids = {edge.edge_id for edge in edge_list}
        for edge in edge_list:
            if edge.source_node_id == edge.target_node_id:
                raise ValueError(f"graph edge {edge.edge_id} is self-referential")
            # Shared edges (course_id is None) are not part of any one course's
            # previous-edge set, but a human review of one must still survive.
            edge_previous = previous_edges.get(edge.edge_id) or self.session.get(
                GraphEdgeRow, edge.edge_id
            )
            if edge_previous is not None:
                if edge_previous.created_at:
                    edge.created_at = parse_dt(edge_previous.created_at) or edge.created_at
                # A human review survives a rebuild; the rebuilt facts replace
                # only the inferred/structural part of the record.
                previous_status = GraphEdgeStatus(edge_previous.status)
                existing_meta = loads(edge_previous.metadata_json, {})
                if isinstance(existing_meta, dict):
                    prior_history = existing_meta.get("review_history")
                    if isinstance(prior_history, list):
                        history = [*prior_history]
                        current_history = edge.metadata.get("review_history")
                        if isinstance(current_history, list):
                            for item in current_history:
                                if item not in history:
                                    history.append(item)
                        edge.metadata["review_history"] = history[-50:]
                    if existing_meta.get("origin") == "phase6_discovery":
                        edge.metadata.setdefault("origin", "phase6_discovery")
                        for key in (
                            "prerequisite_evidence",
                            "limitations",
                            "syllabus_ids",
                            "unit_ids",
                            "document_ids",
                            "distances",
                            "evidence_sources",
                            "evidence_methods",
                            "algorithm",
                            "algorithm_version",
                            "review_required",
                        ):
                            if key in existing_meta and key not in edge.metadata:
                                edge.metadata[key] = existing_meta[key]
                        if existing_meta.get("disabled"):
                            edge.metadata["disabled"] = True
                        derived_evidence = loads(edge_previous.evidence_json, [])
                        if isinstance(derived_evidence, list):
                            edge.evidence = sorted(
                                {*edge.evidence, *[str(item) for item in derived_evidence]}
                            )
                if previous_status in (GraphEdgeStatus.REVIEWED, GraphEdgeStatus.VERIFIED):
                    edge.status = previous_status
                    reviewed_evidence = loads(edge_previous.evidence_json, [])
                    if isinstance(reviewed_evidence, list):
                        edge.evidence = sorted(
                            {*edge.evidence, *[str(item) for item in reviewed_evidence]}
                        )
                    if isinstance(existing_meta, dict):
                        for key in (
                            "reviewed_at",
                            "reviewed_by",
                            "review_note",
                            "validated",
                            "validated_by",
                        ):
                            if key in existing_meta:
                                edge.metadata[key] = existing_meta[key]
                    edge.updated_at = parse_dt(edge_previous.updated_at) or edge.updated_at
            self.session.merge(mappers.edge_to_row(edge))

        # Phase 6 candidates are derived after the structural build. Preserve
        # them across rebuilds when both endpoints still exist, so a human
        # review or provenance trail is not silently destroyed. Stale
        # candidates whose concepts disappeared are dropped with the old nodes.
        preserved = 0
        for edge_id, row in previous_edges.items():
            if edge_id in new_edge_ids:
                continue
            if row.relationship != GraphRelationship.PREREQUISITE_OF.value:
                continue
            metadata = loads(row.metadata_json, {})
            if not isinstance(metadata, dict) or metadata.get("origin") != "phase6_discovery":
                continue
            if row.source_node_id not in node_ids or row.target_node_id not in node_ids:
                continue
            edge = mappers.row_to_edge(row)
            kept = dict(edge.metadata)
            kept["preserved_on_rebuild"] = True
            kept["preserved_at"] = iso(utcnow()) or ""
            edge = edge.model_copy(update={"metadata": kept, "updated_at": utcnow()})
            self.session.merge(mappers.edge_to_row(edge))
            preserved += 1
        self.session.flush()
        self._prune_orphan_shared_nodes()
        return preserved

    def _prune_orphan_shared_nodes(self) -> None:
        """Remove shared institution/subject rows that no course references.

        A shared ``subject -> institution`` edge would otherwise keep both
        endpoints alive forever, including after the last course using them is
        reset. Course-scoped edges are therefore the source of truth for whether
        a shared node is still in use.
        """
        referenced: set[str] = set()
        for source_id, target_id in self.session.execute(
            select(GraphEdgeRow.source_node_id, GraphEdgeRow.target_node_id).where(
                GraphEdgeRow.course_id.is_not(None)
            )
        ).all():
            referenced.add(source_id)
            referenced.add(target_id)

        shared_edges = self.session.scalars(
            select(GraphEdgeRow).where(GraphEdgeRow.course_id.is_(None))
        ).all()
        for edge in shared_edges:
            if edge.source_node_id not in referenced or edge.target_node_id not in referenced:
                self.session.delete(edge)
        self.session.flush()

        has_edge = exists().where(
            or_(
                GraphEdgeRow.source_node_id == GraphNodeRow.node_id,
                GraphEdgeRow.target_node_id == GraphNodeRow.node_id,
            )
        )
        self.session.execute(
            delete(GraphNodeRow).where(
                GraphNodeRow.course_id.is_(None),
                not_(has_edge),
            )
        )
        self.session.flush()

    def clear(self, course_id: str | None = None) -> None:
        if course_id is None:
            self.session.execute(delete(GraphEdgeRow))
            self.session.execute(delete(GraphNodeRow))
        else:
            self.session.execute(delete(GraphEdgeRow).where(GraphEdgeRow.course_id == course_id))
            self.session.execute(delete(GraphNodeRow).where(GraphNodeRow.course_id == course_id))
        self.session.flush()
        self._prune_orphan_shared_nodes()

    def delete_node(self, node_id: str) -> bool:
        row = self.session.get(GraphNodeRow, node_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        self._prune_orphan_shared_nodes()
        return True

    def delete_edge(self, edge_id: str) -> bool:
        row = self.session.get(GraphEdgeRow, edge_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def upsert_edge(self, edge: GraphEdge) -> None:
        """Insert or replace one edge row without deleting its endpoints."""
        self.session.merge(mappers.edge_to_row(edge))
        self.session.flush()

    # -- reads -------------------------------------------------------------

    def get_node(self, node_id: str) -> GraphNode | None:
        row = self.session.get(GraphNodeRow, node_id)
        return mappers.row_to_node(row) if row else None

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        row = self.session.get(GraphEdgeRow, edge_id)
        return mappers.row_to_edge(row) if row else None

    def list_nodes(
        self,
        *,
        course_id: str | None = None,
        node_type: GraphNodeType | None = None,
        syllabus_id: str | None = None,
        document_id: str | None = None,
        question_id: str | None = None,
        entity_id: str | None = None,
        normalized_label: str | None = None,
        limit: int | None = None,
    ) -> list[GraphNode]:
        stmt = select(GraphNodeRow)
        if course_id is not None:
            stmt = stmt.where(GraphNodeRow.course_id == course_id)
        if node_type is not None:
            stmt = stmt.where(GraphNodeRow.node_type == node_type.value)
        if syllabus_id is not None:
            stmt = stmt.where(GraphNodeRow.syllabus_id == syllabus_id)
        if document_id is not None:
            stmt = stmt.where(GraphNodeRow.document_id == document_id)
        if question_id is not None:
            stmt = stmt.where(GraphNodeRow.question_id == question_id)
        if entity_id is not None:
            stmt = stmt.where(GraphNodeRow.entity_id == entity_id)
        if normalized_label is not None:
            stmt = stmt.where(GraphNodeRow.normalized_label == normalized_label)
        stmt = stmt.order_by(
            GraphNodeRow.node_type, func.lower(GraphNodeRow.label), GraphNodeRow.node_id
        )
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_node(row) for row in self.session.scalars(stmt).all()]

    def list_edges(
        self,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        relationship: GraphRelationship | None = None,
        source_node_id: str | None = None,
        target_node_id: str | None = None,
        status: GraphEdgeStatus | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> list[GraphEdge]:
        stmt = select(GraphEdgeRow)
        if course_id is not None:
            stmt = stmt.where(GraphEdgeRow.course_id == course_id)
        if syllabus_id is not None:
            stmt = stmt.where(GraphEdgeRow.syllabus_id == syllabus_id)
        if relationship is not None:
            stmt = stmt.where(GraphEdgeRow.relationship == relationship.value)
        if source_node_id is not None:
            stmt = stmt.where(GraphEdgeRow.source_node_id == source_node_id)
        if target_node_id is not None:
            stmt = stmt.where(GraphEdgeRow.target_node_id == target_node_id)
        if status is not None:
            stmt = stmt.where(GraphEdgeRow.status == status.value)
        if min_confidence is not None:
            stmt = stmt.where(
                GraphEdgeRow.confidence.is_not(None),
                GraphEdgeRow.confidence >= min_confidence,
            )
        stmt = stmt.order_by(
            GraphEdgeRow.relationship, GraphEdgeRow.source_node_id, GraphEdgeRow.target_node_id
        )
        if limit:
            stmt = stmt.limit(limit)
        return [mappers.row_to_edge(row) for row in self.session.scalars(stmt).all()]

    def status_nodes(
        self, *, course_id: str | None = None
    ) -> list[dict[str, object]]:
        """Return only the fields needed by graph status, without ORM objects.

        Status is requested on every dashboard bootstrap. Loading and mapping
        full node models for all courses is unnecessarily expensive; this
        projection keeps the same course scope and values.
        """
        stmt = select(
            GraphNodeRow.node_id,
            GraphNodeRow.course_id,
            GraphNodeRow.node_type,
            GraphNodeRow.label,
        )
        if course_id is not None:
            stmt = stmt.where(GraphNodeRow.course_id == course_id)
        rows = self.session.execute(stmt).all()
        return [
            {
                "node_id": str(row.node_id),
                "course_id": row.course_id,
                "node_type": row.node_type,
                "label": row.label or "",
            }
            for row in rows
        ]

    def status_edges(
        self, *, course_id: str | None = None
    ) -> list[dict[str, object]]:
        """Return only the fields needed by graph status, without ORM objects."""
        stmt = select(
            GraphEdgeRow.course_id,
            GraphEdgeRow.source_node_id,
            GraphEdgeRow.target_node_id,
            GraphEdgeRow.status,
        )
        if course_id is not None:
            stmt = stmt.where(GraphEdgeRow.course_id == course_id)
        rows = self.session.execute(stmt).all()
        return [
            {
                "course_id": row.course_id,
                "source_node_id": row.source_node_id,
                "target_node_id": row.target_node_id,
                "status": row.status,
            }
            for row in rows
        ]

    def neighbors(
        self,
        node_id: str,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        relationships: set[GraphRelationship] | None = None,
        direction: Direction = "both",
    ) -> list[GraphEdge]:
        if direction == "out":
            stmt = select(GraphEdgeRow).where(GraphEdgeRow.source_node_id == node_id)
        elif direction == "in":
            stmt = select(GraphEdgeRow).where(GraphEdgeRow.target_node_id == node_id)
        else:
            stmt = select(GraphEdgeRow).where(
                or_(
                    GraphEdgeRow.source_node_id == node_id,
                    GraphEdgeRow.target_node_id == node_id,
                )
            )
        if course_id is not None:
            stmt = stmt.where(GraphEdgeRow.course_id == course_id)
        if syllabus_id is not None:
            stmt = stmt.where(GraphEdgeRow.syllabus_id == syllabus_id)
        if relationships:
            stmt = stmt.where(
                GraphEdgeRow.relationship.in_(
                    [relationship.value for relationship in relationships]
                )
            )
        stmt = stmt.order_by(GraphEdgeRow.relationship, GraphEdgeRow.edge_id)
        return [mappers.row_to_edge(row) for row in self.session.scalars(stmt).all()]

    def list_isolated_nodes(
        self, course_id: str, *, node_type: GraphNodeType | None = None
    ) -> list[GraphNode]:
        has_edge = exists().where(
            or_(
                GraphEdgeRow.source_node_id == GraphNodeRow.node_id,
                GraphEdgeRow.target_node_id == GraphNodeRow.node_id,
            )
        )
        stmt = select(GraphNodeRow).where(
            GraphNodeRow.course_id == course_id,
            not_(has_edge),
        )
        if node_type is not None:
            stmt = stmt.where(GraphNodeRow.node_type == node_type.value)
        stmt = stmt.order_by(GraphNodeRow.node_type, func.lower(GraphNodeRow.label))
        return [mappers.row_to_node(row) for row in self.session.scalars(stmt).all()]

    def count_nodes(
        self, *, course_id: str | None = None, node_type: GraphNodeType | None = None
    ) -> int:
        stmt = select(func.count()).select_from(GraphNodeRow)
        if course_id is not None:
            stmt = stmt.where(GraphNodeRow.course_id == course_id)
        if node_type is not None:
            stmt = stmt.where(GraphNodeRow.node_type == node_type.value)
        return int(self.session.scalar(stmt) or 0)

    def count_edges(
        self,
        *,
        course_id: str | None = None,
        syllabus_id: str | None = None,
        relationship: GraphRelationship | None = None,
        status: GraphEdgeStatus | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(GraphEdgeRow)
        if course_id is not None:
            stmt = stmt.where(GraphEdgeRow.course_id == course_id)
        if syllabus_id is not None:
            stmt = stmt.where(GraphEdgeRow.syllabus_id == syllabus_id)
        if relationship is not None:
            stmt = stmt.where(GraphEdgeRow.relationship == relationship.value)
        if status is not None:
            stmt = stmt.where(GraphEdgeRow.status == status.value)
        return int(self.session.scalar(stmt) or 0)

    def has_dangling_edges(self, *, course_id: str | None = None) -> bool:
        source_exists = exists(
            select(GraphNodeRow.node_id).where(GraphNodeRow.node_id == GraphEdgeRow.source_node_id)
        )
        target_exists = exists(
            select(GraphNodeRow.node_id).where(GraphNodeRow.node_id == GraphEdgeRow.target_node_id)
        )
        stmt = select(GraphEdgeRow.edge_id).where(or_(not_(source_exists), not_(target_exists)))
        if course_id is not None:
            stmt = stmt.where(GraphEdgeRow.course_id == course_id)
        return self.session.scalars(stmt.limit(1)).first() is not None

    def update_edge_status(
        self,
        edge_id: str,
        *,
        status: GraphEdgeStatus,
        evidence: list[str] | None = None,
    ) -> bool:
        row = self.session.get(GraphEdgeRow, edge_id)
        if row is None:
            return False
        previous_status = row.status
        row.status = status.value
        if evidence:
            existing = loads(row.evidence_json, [])
            if not isinstance(existing, list):
                existing = []
            row.evidence_json = dumps(sorted({str(item) for item in existing} | set(evidence)))
        metadata = loads(row.metadata_json, {})
        if not isinstance(metadata, dict):
            metadata = {}
        if status in (
            GraphEdgeStatus.EXPLICIT,
            GraphEdgeStatus.REVIEWED,
            GraphEdgeStatus.VERIFIED,
        ):
            if status in (GraphEdgeStatus.REVIEWED, GraphEdgeStatus.VERIFIED):
                metadata.setdefault("reviewed_at", iso(utcnow()))
                metadata["validated_by"] = "human_review"
            else:
                # Explicit means "stated by the source"; it is validated, but
                # it is not a human-review decision and must not claim to be.
                for key in ("reviewed_at", "reviewed_by", "review_note"):
                    metadata.pop(key, None)
                metadata.pop("validated_by", None)
            if row.relationship == GraphRelationship.PREREQUISITE_OF.value:
                metadata["validated"] = True
                metadata["review_required"] = False
        else:
            # A correction can downgrade an edge. Remove the current review
            # markers, but retain the decision trail in review_history so the
            # change remains auditable.
            for key in ("reviewed_at", "reviewed_by", "review_note"):
                metadata.pop(key, None)
            if row.relationship == GraphRelationship.PREREQUISITE_OF.value:
                metadata["validated"] = False
                metadata["review_required"] = True
                metadata.pop("validated_by", None)
        if previous_status != status.value:
            history = metadata.get("review_history")
            if not isinstance(history, list):
                history = []
            history.append({"from": previous_status, "to": status.value, "at": iso(utcnow()) or ""})
            metadata["review_history"] = history[-50:]
        row.metadata_json = dumps(metadata)
        row.updated_at = iso(utcnow()) or row.updated_at
        self.session.flush()
        return True


__all__ = ["Direction", "GraphRepository"]
