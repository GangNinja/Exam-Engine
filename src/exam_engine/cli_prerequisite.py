"""``exam-engine prerequisites`` - discover, review and plan Phase 6 paths.

The command layer is intentionally thin: discovery, validation and path planning
live in replaceable services that a dashboard or another agent can call directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import typer

from exam_engine.config import Settings
from exam_engine.graph.service import GraphService
from exam_engine.models.enums import GraphEdgeStatus, GraphNodeType
from exam_engine.models.prerequisite import (
    LearningPathRequest,
    LearningPathStatus,
    PrerequisiteDiscoveryRequest,
)
from exam_engine.prerequisite.discovery import PrerequisiteDiscoveryService
from exam_engine.prerequisite.path import LearningPathService
from exam_engine.storage.db import Database
from exam_engine.storage.repositories import CourseRepository

OpenDatabase = Callable[[], Database]
GetSettings = Callable[[], Settings]


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        typer.echo("(no rows)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    typer.echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _short(value: str, limit: int = 60) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _dump(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def register_prerequisite_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(help="Phase 6 prerequisite discovery, candidate review and learning paths.")

    def _graph(database: Database) -> GraphService:
        return GraphService(database, get_settings().graph)

    def _discovery(database: Database) -> PrerequisiteDiscoveryService:
        return PrerequisiteDiscoveryService(_graph(database), get_settings().prerequisite)

    def _paths(database: Database) -> LearningPathService:
        return LearningPathService(_graph(database), get_settings().prerequisite)

    def _require_course(database: Database, course: str) -> None:
        with database.session() as session:
            if CourseRepository(session).get(course) is None:
                raise ValueError(f"unknown course: {course}")

    def _resolve_target(
        database: Database,
        course: str,
        value: str,
        node_type: GraphNodeType,
    ) -> str:
        _require_course(database, course)
        graph = _graph(database)
        matches = graph.find_nodes(course, value, node_type=node_type)
        if not matches:
            raise ValueError(f"unknown {node_type.value} target {value!r} in course {course}")
        exact = [node for node in matches if node.node_id == value or node.entity_id == value]
        if len(exact) == 1:
            return exact[0].node_id
        if len(matches) == 1:
            return matches[0].node_id
        listed = ", ".join(f"{node.node_id} ({_short(node.label, 32)})" for node in matches)
        raise ValueError(f"ambiguous target {value!r}; pass one node id: {listed}")

    @group.command("discover")
    def discover(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        order: bool = typer.Option(True, "--order/--no-order", help="Use syllabus topic order."),
        questions: bool = typer.Option(
            True, "--questions/--no-questions", help="Attach question-association evidence."
        ),
        cooccurrence: bool = typer.Option(
            True, "--cooccurrence/--no-cooccurrence", help="Attach repeated co-occurrence evidence."
        ),
        max_cooccurrence: int = typer.Option(
            100,
            "--max-cooccurrence-candidates",
            min=0,
            max=10000,
            help="Cap candidate pairs that receive the extra co-occurrence signal.",
        ),
        persist: bool = typer.Option(
            True, "--persist/--dry-run", help="Write reviewable candidates into the graph."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Create candidate prerequisites; candidates are never automatically verified."""
        database = open_db()
        try:
            report = _discovery(database).discover(
                PrerequisiteDiscoveryRequest(
                    course_id=course,
                    syllabus_id=syllabus,
                    include_order_evidence=order,
                    include_question_evidence=questions,
                    include_cooccurrence=cooccurrence,
                    max_cooccurrence_candidates=max_cooccurrence,
                    persist=persist,
                )
            )
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump(report.model_dump(mode="json"))
            return
        typer.echo(
            f"course={report.course_id} syllabus={report.syllabus_id or '-'} "
            f"nodes={report.nodes_scanned} existing={report.edges_existing} "
            f"created={report.edges_created} updated={report.edges_updated} "
            f"candidates={report.candidate_count}"
        )
        _print_table(
            ["status", "source", "target", "evidence", "edge"],
            [
                [
                    item.status.value,
                    _short(item.source.label, 30),
                    _short(item.target.label, 30),
                    len(item.evidence),
                    item.edge_id,
                ]
                for item in report.candidates
            ],
        )
        if report.cycles:
            typer.echo(f"cycles={len(report.cycles)} (preserved for review)")
        if report.conflicts:
            typer.echo(f"conflicts={len(report.conflicts)}")
        for limitation in report.limitations:
            typer.echo(f"note: {limitation}")
        typer.echo(
            "note: candidates are reviewable proposals, not verified prerequisites; "
            "use 'prerequisites review' or 'graph review'."
        )

    @group.command("candidates")
    def candidates(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        concept: str | None = typer.Option(None, "--concept", help="Node id filter."),
        status: GraphEdgeStatus | None = typer.Option(None, "--status"),
        include_disabled: bool = typer.Option(False, "--include-disabled"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List PREREQUISITE_OF edges with status and evidence counts."""
        database = open_db()
        try:
            _require_course(database, course)
            graph = _graph(database)
            edges = graph.prerequisite_edges(
                course,
                syllabus_id=syllabus,
                include_candidates=True,
                include_inferred=True,
                include_disabled=include_disabled,
            )
            if concept:
                edges = [
                    edge for edge in edges if concept in (edge.source_node_id, edge.target_node_id)
                ]
            if status:
                edges = [edge for edge in edges if edge.status is status]
            labels = {node.node_id: node.label for node in graph.concepts(course)}
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump([edge.model_dump(mode="json") for edge in edges])
            return
        _print_table(
            ["status", "source", "target", "method", "conf", "edge"],
            [
                [
                    edge.status.value,
                    _short(labels.get(edge.source_node_id, edge.source_node_id), 28),
                    _short(labels.get(edge.target_node_id, edge.target_node_id), 28),
                    _short(edge.method, 40),
                    "-" if edge.confidence is None else f"{edge.confidence:.3f}",
                    edge.edge_id,
                ]
                for edge in edges
            ],
        )

    @group.command("validate")
    def validate(
        course: str = typer.Option(..., "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        include_candidates: bool = typer.Option(True, "--include-candidates/--supported-only"),
        include_inferred: bool = typer.Option(True, "--include-inferred/--no-inferred"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Detect cycles, reverse conflicts and isolated concepts without deleting edges."""
        database = open_db()
        try:
            report = _discovery(database).validate(
                course,
                syllabus_id=syllabus,
                include_candidates=include_candidates,
                include_inferred=include_inferred,
            )
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump(report.model_dump(mode="json"))
        else:
            typer.echo(
                f"relationships={report.relationship_count} "
                f"supported={report.supported_count} candidates={report.candidate_count} "
                f"cycles={len(report.cycles)} conflicts={len(report.conflicts)} "
                f"isolated={len(report.isolated_concepts)}"
            )
            for cycle in report.cycles:
                typer.echo(f"cycle: {cycle.message} edges={','.join(cycle.edge_ids)}")
            for conflict in report.conflicts:
                typer.echo(f"conflict: {conflict.message} edges={','.join(conflict.edge_ids)}")
            for concept in report.isolated_concepts:
                typer.echo(f"isolated: {_short(concept.label, 55)} {concept.concept_id}")
            for limitation in report.limitations:
                typer.echo(f"note: {limitation}")
            typer.echo("integrity: " + ("OK" if report.valid else "REVIEW REQUIRED"))
        if not report.valid:
            raise typer.Exit(code=1)

    @group.command("path")
    def path(
        course: str = typer.Option(..., "--course"),
        target: str = typer.Option(..., "--target", help="Node id or label."),
        target_type: GraphNodeType = typer.Option(
            GraphNodeType.CONCEPT, "--target-type", help="concept, topic or unit."
        ),
        syllabus: str | None = typer.Option(None, "--syllabus"),
        known: list[str] | None = typer.Option(
            None, "--known", help="Known/mastered concept id or label; repeatable."
        ),
        include_candidates: bool = typer.Option(
            False,
            "--include-candidates",
            help="Include unverified candidates (marks plan partial).",
        ),
        include_inferred: bool = typer.Option(
            False, "--include-inferred", help="Include model-inferred edges (marks plan partial)."
        ),
        review_known: bool = typer.Option(
            False, "--review-known", help="Show mastered concepts as review steps."
        ),
        max_depth: int | None = typer.Option(None, "--max-depth", min=1, max=32),
        max_length: int | None = typer.Option(None, "--max-length", min=1, max=200),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Generate an explainable dependency-ordered learning path."""
        database = open_db()
        try:
            target_id = _resolve_target(database, course, target, target_type)
            known_ids: list[str] = []
            for item in known or []:
                try:
                    known_ids.append(_resolve_target(database, course, item, GraphNodeType.CONCEPT))
                except ValueError as exc:
                    typer.echo(f"ERROR: {exc}", err=True)
                    raise typer.Exit(code=1) from exc
            plan = _paths(database).generate(
                LearningPathRequest(
                    course_id=course,
                    target_node_id=target_id,
                    target_node_type=target_type,
                    syllabus_id=syllabus,
                    known_concept_ids=known_ids,
                    include_candidates=include_candidates,
                    include_inferred=include_inferred,
                    review_known=review_known,
                    max_depth=max_depth or get_settings().prerequisite.path_max_depth,
                    max_path_length=max_length,
                )
            )
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump(plan.model_dump(mode="json"))
        else:
            typer.echo(
                f"target={_short(str(plan.target_node_id), 50)} type={plan.target_node_type.value} "
                f"status={plan.status} steps={len(plan.steps)} syllabus={plan.syllabus_id or '-'}"
            )
            for step in plan.steps:
                known_mark = " [known]" if step.already_known else ""
                confidence = "-" if step.confidence is None else f"{step.confidence:.3f}"
                typer.echo(
                    f"{step.position:>2}. [{step.role}] {_short(step.label, 55)}{known_mark} "
                    f"depth={step.depth} status={step.status.value if step.status else '-'} "
                    f"confidence={confidence}"
                )
                typer.echo(f"    why: {step.rationale}")
                for evidence in step.evidence:
                    page = f" p.{evidence.source_page}" if evidence.source_page else ""
                    document = (
                        f" doc={evidence.source_document_id}" if evidence.source_document_id else ""
                    )
                    typer.echo(f"    evidence: {evidence.detail}{document}{page}")
                if step.edge_ids:
                    typer.echo(f"    edges: {', '.join(step.edge_ids)}")
            if plan.known_concepts_skipped:
                typer.echo("known/skipped:")
                for concept in plan.known_concepts_skipped:
                    typer.echo(f"  - {_short(concept.label, 55)} {concept.concept_id}")
            if plan.unresolved_concepts:
                typer.echo("unresolved/missing:")
                for concept in plan.unresolved_concepts:
                    typer.echo(f"  - {_short(concept.label, 55)} {concept.concept_id}")
            for cycle in plan.cycles:
                typer.echo(f"cycle broken for ordering: {cycle.message}")
            for conflict in plan.conflicts:
                typer.echo(f"conflict: {conflict.message}")
            for missing in plan.missing_metadata:
                typer.echo(f"metadata: {missing}")
            for limitation in plan.limitations:
                typer.echo(f"note: {limitation}")
        if plan.status == LearningPathStatus.INVALID_TARGET.value:
            raise typer.Exit(code=1)

    @group.command("review")
    def review(
        edge_id: str = typer.Argument(...),
        status: GraphEdgeStatus = typer.Option(
            ..., "--status", help="candidate, inferred, explicit, reviewed or verified."
        ),
        evidence: list[str] | None = typer.Option(
            None, "--evidence", help="Review note/evidence; repeatable."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Record a human review decision; the original evidence is retained."""
        database = open_db()
        try:
            graph = _graph(database)
            edge = graph.edge(edge_id)
            if edge.relationship.value != "PREREQUISITE_OF":
                typer.echo(f"ERROR: edge {edge_id} is not PREREQUISITE_OF", err=True)
                raise typer.Exit(code=1)
            changed = graph.set_edge_status(edge_id, status, evidence=evidence)
        except KeyError as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if not changed:
            typer.echo(f"ERROR: unknown graph edge: {edge_id}", err=True)
            raise typer.Exit(code=1)
        if as_json:
            _dump({"edge_id": edge_id, "status": status.value, "updated": True})
            return
        typer.echo(f"updated {edge_id} -> {status.value}; prior evidence preserved")

    @group.command("disable")
    def disable(
        edge_id: str = typer.Argument(...),
        note: str | None = typer.Option(None, "--note"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Disable an edge without deleting its evidence or review history."""
        _set_disabled(edge_id, disabled=True, note=note, as_json=as_json)

    @group.command("enable")
    def enable(
        edge_id: str = typer.Argument(...),
        note: str | None = typer.Option(None, "--note"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Re-enable a previously disabled edge."""
        _set_disabled(edge_id, disabled=False, note=note, as_json=as_json)

    def _set_disabled(edge_id: str, *, disabled: bool, note: str | None, as_json: bool) -> None:
        database = open_db()
        try:
            changed = _graph(database).set_edge_disabled(edge_id, disabled=disabled, note=note)
        finally:
            database.close()
        if not changed:
            typer.echo(f"ERROR: unknown graph edge: {edge_id}", err=True)
            raise typer.Exit(code=1)
        if as_json:
            _dump({"edge_id": edge_id, "disabled": disabled, "updated": True})
            return
        typer.echo(f"{'disabled' if disabled else 'enabled'} {edge_id}")

    app.add_typer(group, name="prerequisites")


__all__ = ["register_prerequisite_commands"]
