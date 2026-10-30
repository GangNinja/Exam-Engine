"""Model <-> row conversion for the Phase 5 knowledge-graph tables."""

from __future__ import annotations

from datetime import UTC, datetime

from exam_engine.models.enums import GraphEdgeStatus, GraphNodeType, GraphRelationship
from exam_engine.models.graph import GraphEdge, GraphNode
from exam_engine.storage.mappers import dumps, iso, loads, parse_dt
from exam_engine.storage.schema import GraphEdgeRow, GraphNodeRow


def node_to_row(node: GraphNode) -> GraphNodeRow:
    return GraphNodeRow(
        node_id=node.node_id,
        node_type=node.node_type.value,
        node_key=node.node_key,
        label=node.label,
        normalized_label=node.normalized_label,
        course_id=node.course_id,
        syllabus_id=node.syllabus_id,
        document_id=node.document_id,
        question_id=node.question_id,
        entity_id=node.entity_id,
        metadata_json=dumps(node.metadata),
        created_at=iso(node.created_at) or "",
        updated_at=iso(node.updated_at) or "",
    )


def row_to_node(row: GraphNodeRow) -> GraphNode:
    return GraphNode(
        node_id=row.node_id,
        node_type=GraphNodeType(row.node_type),
        node_key=row.node_key,
        label=row.label,
        normalized_label=row.normalized_label,
        course_id=row.course_id,
        syllabus_id=row.syllabus_id,
        document_id=row.document_id,
        question_id=row.question_id,
        entity_id=row.entity_id,
        metadata=loads(row.metadata_json, {}),
        created_at=parse_dt(row.created_at) or datetime.now(UTC),
        updated_at=parse_dt(row.updated_at) or datetime.now(UTC),
    )


def edge_to_row(edge: GraphEdge) -> GraphEdgeRow:
    return GraphEdgeRow(
        edge_id=edge.edge_id,
        course_id=edge.course_id,
        syllabus_id=edge.syllabus_id,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        relationship=edge.relationship.value,
        status=edge.status.value,
        confidence=edge.confidence,
        method=edge.method,
        evidence_json=dumps(edge.evidence),
        source_document_id=edge.source_document_id,
        source_page=edge.source_page,
        model_version=edge.model_version,
        metadata_json=dumps(edge.metadata),
        created_at=iso(edge.created_at) or "",
        updated_at=iso(edge.updated_at) or "",
    )


def row_to_edge(row: GraphEdgeRow) -> GraphEdge:
    return GraphEdge(
        edge_id=row.edge_id,
        course_id=row.course_id,
        syllabus_id=row.syllabus_id,
        source_node_id=row.source_node_id,
        target_node_id=row.target_node_id,
        relationship=GraphRelationship(row.relationship),
        status=GraphEdgeStatus(row.status),
        confidence=row.confidence,
        method=row.method,
        evidence=loads(row.evidence_json, []),
        source_document_id=row.source_document_id,
        source_page=row.source_page,
        model_version=row.model_version,
        metadata=loads(row.metadata_json, {}),
        created_at=parse_dt(row.created_at) or datetime.now(UTC),
        updated_at=parse_dt(row.updated_at) or datetime.now(UTC),
    )


__all__ = ["edge_to_row", "node_to_row", "row_to_edge", "row_to_node"]
