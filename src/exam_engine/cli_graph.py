"""``exam-engine graph`` - inspect and review the Phase 5 knowledge graph.

Like the Phase 4 CLI this is a thin presenter over :class:`GraphService`. All
queries, scope rules and review semantics live in the service/repository layer so
a later dashboard can reuse them without the command line.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, Literal

import typer

from exam_engine.config import Settings
from exam_engine.graph.service import GraphService
from exam_engine.models.enums import GraphEdgeStatus, GraphNodeType, GraphRelationship
from exam_engine.storage.db import Database

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


def _short(value: str, limit: int = 70) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _dump(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def register_graph_commands(
    app: typer.Typer, *, open_db: OpenDatabase, get_settings: GetSettings
) -> None:
    group = typer.Typer(
        help="Phase 5 persistent knowledge graph: build, query and review evidence."
    )

    def _service(database: Database) -> GraphService:
        return GraphService(database, get_settings().graph)

    @group.command("build")
    def build(
        course: list[str] | None = typer.Option(
            None, "--course", help="Course id; repeatable. Omit to build every course."
        ),
        infer_prerequisites: bool | None = typer.Option(
            None,
            "--infer-prerequisites/--no-infer-prerequisites",
            help="Create review-only PREREQUISITE_OF candidates from syllabus topic order.",
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Rebuild one or more course graphs from current Phase 1-4 rows."""
        database = open_db()
        try:
            service = _service(database)
            if course:
                reports = [
                    service.build_course(item, infer_prerequisites=infer_prerequisites)
                    for item in course
                ]
            else:
                reports = service.build(infer_prerequisites=infer_prerequisites)
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump([report.model_dump(mode="json") for report in reports])
        else:
            _print_table(
                ["course", "status", "nodes", "shared", "edges", "warnings", "ms"],
                [
                    [
                        report.course_id,
                        report.status.value,
                        report.node_count,
                        report.shared_node_count,
                        report.edge_count,
                        len(report.warnings),
                        report.duration_ms,
                    ]
                    for report in reports
                ],
            )
            for report in reports:
                for warning in report.warnings:
                    typer.echo(f"  {warning.severity.value}: {warning.code}: {warning.message}")
                for error in report.errors:
                    typer.echo(f"  error: {error.code}: {error.message}")
        if any(report.status.value == "failed" for report in reports):
            raise typer.Exit(code=1)

    @group.command("status")
    def status(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show node/edge counts and candidate/dangling state per course."""
        database = open_db()
        try:
            rows = _service(database).status()
        finally:
            database.close()
        if as_json:
            _dump(rows)
            return
        _print_table(
            [
                "course",
                "nodes",
                "edges",
                "concepts",
                "questions",
                "units",
                "topics",
                "sessions",
                "candidate",
                "dangling",
            ],
            [
                [
                    row["course_id"],
                    row["nodes"],
                    row["edges"],
                    row["concepts"],
                    row["questions"],
                    row["units"],
                    row["topics"],
                    row["sessions"],
                    row["candidate_edges"],
                    "yes" if row["dangling_edges"] else "no",
                ]
                for row in rows
            ],
        )

    @group.command("concepts")
    def concepts(
        course: str = typer.Option(..., "--course"),
        unit: str | None = typer.Option(None, "--unit", help="Syllabus unit id."),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus id."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List concepts attached to a course, optionally scoped to a unit/syllabus."""
        database = open_db()
        try:
            nodes = _service(database).concepts(course, unit_id=unit, syllabus_id=syllabus)
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump([node.model_dump(mode="json") for node in nodes])
            return
        _print_table(
            ["concept_id", "label", "origins", "verified", "units"],
            [
                [
                    node.node_id,
                    _short(node.label, 48),
                    ",".join(node.metadata.get("origins", [])) or "-",
                    "yes" if node.metadata.get("verified") else "no",
                    len(node.metadata.get("unit_ids", [])),
                ]
                for node in nodes
            ],
        )

    @group.command("questions")
    def questions(
        course: str = typer.Option(..., "--course"),
        topic: str | None = typer.Option(None, "--topic", help="Topic graph node id."),
        concept: str | None = typer.Option(None, "--concept", help="Concept graph node id."),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus id."),
        include_candidates: bool = typer.Option(
            False, "--include-candidates", help="Include unreviewed candidate mappings."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Find questions that cover a topic or assess a concept."""
        if (topic is None) == (concept is None):
            typer.echo("ERROR: provide exactly one of --topic or --concept", err=True)
            raise typer.Exit(code=2)
        database = open_db()
        try:
            nodes = _service(database).questions_for(
                course,
                topic_id=topic,
                concept_id=concept,
                syllabus_id=syllabus,
                include_candidates=include_candidates,
            )
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump([node.model_dump(mode="json") for node in nodes])
            return
        _print_table(
            ["question_id", "question", "document", "marks"],
            [
                [
                    node.node_id,
                    _short(node.label, 55),
                    node.document_id or "-",
                    node.metadata.get("marks") if node.metadata.get("marks") is not None else "-",
                ]
                for node in nodes
            ],
        )

    @group.command("topics")
    def topics(
        course: str = typer.Option(..., "--course"),
        question: str | None = typer.Option(None, "--question", help="Question id."),
        document: str | None = typer.Option(None, "--document", help="Document id."),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus id."),
        include_candidates: bool = typer.Option(
            False, "--include-candidates", help="Include unreviewed candidate mappings."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show topics covered by one question or source document."""
        if (question is None) == (document is None):
            typer.echo("ERROR: provide exactly one of --question or --document", err=True)
            raise typer.Exit(code=2)
        database = open_db()
        try:
            service = _service(database)
            nodes = (
                service.topics_for_question(
                    question,
                    course_id=course,
                    syllabus_id=syllabus,
                    include_candidates=include_candidates,
                )
                if question is not None
                else service.topics_for_document(
                    document or "",
                    course_id=course,
                    syllabus_id=syllabus,
                    include_candidates=include_candidates,
                )
            )
        finally:
            database.close()
        if as_json:
            _dump([node.model_dump(mode="json") for node in nodes])
            return
        _print_table(
            ["topic_id", "topic", "unit"],
            [
                [node.node_id, _short(node.label, 55), node.metadata.get("unit_label") or "-"]
                for node in nodes
            ],
        )

    @group.command("related")
    def related(
        concept_id: str = typer.Argument(...),
        course: str | None = typer.Option(None, "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus id."),
        include_candidates: bool = typer.Option(
            False, "--include-candidates", help="Include unreviewed candidate links."
        ),
        min_confidence: float | None = typer.Option(None, "--min-confidence", min=0.0, max=1.0),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Find RELATED_TO evidence around a concept (candidates excluded by default)."""
        database = open_db()
        try:
            service = _service(database)
            edges = service.related_concepts(
                concept_id,
                course_id=course,
                syllabus_id=syllabus,
                include_candidates=include_candidates,
                min_confidence=min_confidence,
            )
            rows: list[dict[str, Any]] = []
            for edge in edges:
                other_id = (
                    edge.target_node_id
                    if edge.source_node_id == concept_id
                    else edge.source_node_id
                )
                other = service.node(other_id)
                rows.append({"node": other, "edge": edge})
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump(
                [
                    {
                        "concept": row["node"].model_dump(mode="json"),
                        "edge": row["edge"].model_dump(mode="json"),
                    }
                    for row in rows
                ]
            )
            return
        _print_table(
            ["related concept", "status", "confidence", "method", "evidence"],
            [
                [
                    _short(row["node"].label, 42),
                    row["edge"].status.value,
                    f"{row['edge'].confidence:.3f}" if row["edge"].confidence is not None else "-",
                    row["edge"].method,
                    _short("; ".join(row["edge"].evidence), 45),
                ]
                for row in rows
            ],
        )

    @group.command("neighborhood")
    def neighborhood(
        node_id: str = typer.Argument(...),
        course: str | None = typer.Option(None, "--course"),
        syllabus: str | None = typer.Option(None, "--syllabus", help="Syllabus id."),
        hops: int = typer.Option(1, "--hops", min=0, max=10),
        relationship: list[GraphRelationship] | None = typer.Option(
            None, "--relationship", help="Restrict edge types; repeatable."
        ),
        direction: Literal["in", "out", "both"] = typer.Option("both", "--direction"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Explore relationships around a node within a bounded number of hops."""
        database = open_db()
        try:
            value = _service(database).neighborhood(
                node_id,
                hops=hops,
                course_id=course,
                syllabus_id=syllabus,
                relationships=set(relationship) if relationship else None,
                direction=direction,
            )
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump(value.model_dump(mode="json"))
            return
        typer.echo(f"center: {value.center.node_type.value} {value.center.node_id}")
        typer.echo(f"nodes={len(value.nodes)} edges={len(value.edges)} hops={value.hops}")
        _print_table(
            ["type", "label", "node_id"],
            [[node.node_type.value, _short(node.label, 55), node.node_id] for node in value.nodes],
        )
        if value.edges:
            typer.echo("edges:")
            for edge in value.edges:
                typer.echo(
                    f"  {edge.relationship.value} {edge.source_node_id} -> "
                    f"{edge.target_node_id} [{edge.status.value}] {edge.edge_id}"
                )

    @group.command("evidence")
    def evidence(
        edge_id: str = typer.Argument(...),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the source, target, document and evidence behind one relationship."""
        database = open_db()
        try:
            value = _service(database).evidence_for_edge(edge_id)
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump(value.model_dump(mode="json"))
            return
        edge = value.edge
        typer.echo(
            f"{edge.relationship.value} [{edge.status.value}] {edge.edge_id} "
            f"confidence={edge.confidence if edge.confidence is not None else '-'}"
        )
        typer.echo(f"source: {value.source.node_type.value} {value.source.label}")
        typer.echo(f"target: {value.target.node_type.value} {value.target.label}")
        typer.echo(f"method: {edge.method}")
        typer.echo(f"document: {edge.source_document_id or '-'} page={edge.source_page or '-'}")
        typer.echo("evidence:")
        for item in edge.evidence:
            typer.echo(f"  {item}")
        if value.source_document is not None:
            typer.echo(f"source document: {value.source_document.label}")

    @group.command("validate")
    def validate(
        course: str | None = typer.Option(None, "--course"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Check dangling endpoints and course/syllabus scope violations."""
        database = open_db()
        try:
            problems = _service(database).validate(course)
        finally:
            database.close()
        if as_json:
            _dump({"course_id": course, "ok": not problems, "problems": problems})
        elif problems:
            _print_table(["problem"], [[item] for item in problems])
        else:
            typer.echo("graph integrity: OK")
        if problems:
            raise typer.Exit(code=1)

    @group.command("duplicates")
    def duplicates(
        course: str = typer.Option(..., "--course"),
        threshold: float | None = typer.Option(None, "--threshold", min=0.0, max=1.0),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List review-only lexical concept duplicate candidates; nothing is merged."""
        database = open_db()
        try:
            candidates = _service(database).concept_duplicates(course, threshold=threshold)
        except (KeyError, ValueError) as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        if as_json:
            _dump([candidate.model_dump(mode="json") for candidate in candidates])
            return
        _print_table(
            ["score", "left", "right", "method"],
            [
                [
                    f"{candidate.score:.3f}",
                    _short(candidate.left_label, 38),
                    _short(candidate.right_label, 38),
                    candidate.method,
                ]
                for candidate in candidates
            ],
        )
        typer.echo("note: candidates require human review; no concepts were merged.")

    @group.command("isolated")
    def isolated(
        course: str = typer.Option(..., "--course"),
        node_type: GraphNodeType | None = typer.Option(None, "--type"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List course nodes with no relationship at all."""
        database = open_db()
        try:
            nodes = _service(database).isolated_nodes(course, node_type=node_type)
        finally:
            database.close()
        if as_json:
            _dump([node.model_dump(mode="json") for node in nodes])
            return
        _print_table(
            ["type", "label", "node_id"],
            [[node.node_type.value, _short(node.label, 55), node.node_id] for node in nodes],
        )

    @group.command("unmapped")
    def unmapped(
        course: str = typer.Option(..., "--course"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List questions with no ASSESSES edge to a supported concept."""
        database = open_db()
        try:
            nodes = _service(database).questions_without_concepts(course)
        finally:
            database.close()
        if as_json:
            _dump([node.model_dump(mode="json") for node in nodes])
            return
        _print_table(
            ["question_id", "question", "document"],
            [[node.node_id, _short(node.label, 60), node.document_id or "-"] for node in nodes],
        )

    @group.command("review")
    def review(
        edge_id: str = typer.Argument(...),
        status: GraphEdgeStatus = typer.Option(..., "--status"),
        evidence: list[str] | None = typer.Option(
            None, "--evidence", help="Review note/evidence; repeatable."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Record a human review decision for one edge; rebuilds preserve it."""
        database = open_db()
        try:
            changed = _service(database).set_edge_status(edge_id, status, evidence=evidence)
        finally:
            database.close()
        if not changed:
            typer.echo(f"ERROR: unknown graph edge: {edge_id}", err=True)
            raise typer.Exit(code=1)
        if as_json:
            _dump({"edge_id": edge_id, "status": status.value, "updated": True})
            return
        typer.echo(f"updated {edge_id} -> {status.value}")

    @group.command("delete-edge")
    def delete_edge(
        edge_id: str = typer.Argument(...),
        yes: bool = typer.Option(False, "--yes", help="Confirm deletion."),
    ) -> None:
        """Delete one relationship while keeping both endpoint nodes."""
        if not yes:
            typer.echo("ERROR: pass --yes to delete an edge", err=True)
            raise typer.Exit(code=2)
        database = open_db()
        try:
            deleted = _service(database).delete_edge(edge_id)
        finally:
            database.close()
        if not deleted:
            typer.echo(f"ERROR: unknown graph edge: {edge_id}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"deleted {edge_id}")

    @group.command("reset")
    def reset(
        course: str = typer.Option(..., "--course"),
        yes: bool = typer.Option(False, "--yes", help="Confirm deletion of this course graph."),
    ) -> None:
        """Remove one course graph; shared nodes are pruned when unreferenced."""
        if not yes:
            typer.echo("ERROR: pass --yes to reset a course graph", err=True)
            raise typer.Exit(code=2)
        database = open_db()
        try:
            _service(database).reset(course)
        except KeyError as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            database.close()
        typer.echo(f"reset graph for {course}")

    app.add_typer(group, name="graph")


__all__ = ["register_graph_commands"]
