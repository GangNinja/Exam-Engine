"""Phase 13 CLI: discover, audit and import personal examination datasets."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from exam_engine.datasets.queries import ExamDatasetQueries, ExamFilters
from exam_engine.datasets.service import ExamDatasetService
from exam_engine.models.enums import DuplicatePolicy


def _echo(message: str = "", *, err: bool = False) -> None:
    """Write console-safe text; Windows cp1252 must not crash on syllabus content."""
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, "backslashreplace").decode(encoding)
    typer.echo(safe, err=err)


def register_exams_commands(app: typer.Typer, *, open_db: Any, get_settings: Any) -> None:
    exams = typer.Typer(
        add_completion=False,
        help="Phase 13 personal GATE/CBSE/JEE/NEET dataset integration.",
    )

    @exams.command("discover")
    def discover_command(
        root: Path = typer.Option(Path("datasets"), "--root", help="Dataset root directory."),
        as_json: bool = typer.Option(False, "--json", help="Print JSON."),
    ) -> None:
        """List examination categories, subjects and matched syllabus documents."""
        database = open_db()
        try:
            catalog = ExamDatasetService(database, get_settings()).discover(root)
        finally:
            database.close()
        totals = {
            "categories": len(catalog.categories),
            "subjects": len(catalog.subjects),
            "csv": len(catalog.csv_files),
            "pdf": len(catalog.syllabus_files),
        }
        payload = {
            "root": catalog.root.as_posix(),
            "categories": [
                {
                    "category_id": category.category_id,
                    "name": category.name,
                    "subjects": len(catalog.subjects_for_category(category.category_id)),
                }
                for category in catalog.categories
            ],
            "subjects": [
                {
                    "course_id": subject.course_id,
                    "course_code": subject.course_code,
                    "category_id": subject.category_id,
                    "subject": subject.subject_name,
                    "csv": subject.csv_path.as_posix(),
                    "syllabus": [path.as_posix() for path in subject.syllabus_paths],
                    "syllabus_version": subject.syllabus_version,
                }
                for subject in catalog.subjects
            ],
            "totals": totals,
        }
        if as_json:
            _echo(json.dumps(payload, indent=2))
            return
        _echo(
            f"categories={totals['categories']} "
            f"subjects={totals['subjects']} "
            f"csv={totals['csv']} pdf={totals['pdf']}"
        )
        for subject in catalog.subjects:
            syllabus = ", ".join(path.name for path in subject.syllabus_paths) or "NO SYLLABUS"
            _echo(
                f"  {subject.category_id:<14} {subject.course_id:<48} "
                f"{subject.csv_path.name}  ->  {syllabus}"
            )

    @exams.command("audit")
    def audit_command(
        root: Path = typer.Option(Path("datasets"), "--root"),
        out: Path = typer.Option(Path("reports/phase13"), "--out"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Audit CSV rows and syllabus PDFs without changing the dataset."""
        database = open_db()
        try:
            result = ExamDatasetService(database, get_settings()).audit(root, out)
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(result.summary, indent=2))
            return
        summary = result.summary
        _echo(
            f"files={summary['files']['discovered']} csv={summary['files']['csv']} "
            f"pdf={summary['files']['pdf']} rows={summary['records']['csv_rows']}"
        )
        _echo(
            f"questions={summary['records']['questions_with_text']} "
            f"answers={summary['records']['answers_present']} "
            f"years={summary['examination_years']['count']} "
            f"syllabus_units={summary['syllabus']['units_detected']}"
        )
        validation = summary["validation"]
        _echo(
            f"errors={validation['error']} warnings={validation['warning']} "
            f"info={validation['info']}"
        )
        _echo(f"wrote {out / 'dataset_inventory.csv'}")
        _echo(f"wrote {out / 'dataset_validation_report.csv'}")
        _echo(f"wrote {out / 'dataset_summary.json'}")

    @exams.command("import")
    def import_command(
        root: Path = typer.Option(Path("datasets"), "--root"),
        out: Path = typer.Option(Path("reports/phase13"), "--out"),
        category: list[str] = typer.Option([], "--category", help="Limit to category ids."),
        subject: list[str] = typer.Option([], "--subject", help="Limit to course ids/subjects."),
        no_syllabus: bool = typer.Option(False, "--no-syllabus"),
        on_duplicate: DuplicatePolicy = typer.Option(
            DuplicatePolicy.SKIP, "--on-duplicate", case_sensitive=False
        ),
        dry_run: bool = typer.Option(False, "--dry-run"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Import the real examination CSVs and syllabus PDFs into this database."""
        database = open_db()
        try:
            report = ExamDatasetService(database, get_settings()).import_dataset(
                root,
                output_dir=out,
                categories=list(category) or None,
                subjects=list(subject) or None,
                with_syllabus=not no_syllabus,
                on_duplicate=on_duplicate,
                dry_run=dry_run,
            )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(report.as_dict(), indent=2))
            return
        _echo(
            f"run {report.run_id} status={report.status} files={len(report.files)} "
            f"questions={report.totals['questions']} answers={report.totals['answers']} "
            f"syllabus_units={report.totals['syllabus_units']} "
            f"skipped={report.totals['skipped']} ({report.duration_ms} ms)"
        )
        for item in report.files:
            marker = "OK" if item.status in {"ingested", "replaced"} else item.status.upper()
            _echo(
                f"  {marker:<12} {item.relative_path} "
                f"q={item.imported_questions} a={item.imported_answers} "
                f"units={item.imported_syllabus_units} "
                f"skipped={item.skipped_count} {item.duration_ms}ms"
            )
            for warning in item.warnings[:3]:
                _echo(f"      warning: {warning}")
            for error in item.errors[:3]:
                _echo(f"      error: {error}")
        for warning in report.warnings[:5]:
            _echo(f"  note: {warning}")
        _echo(f"report: {report.output_path}")

    @exams.command("status")
    def status_command(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show stored exam counts and the latest import run."""
        database = open_db()
        try:
            payload = ExamDatasetService(database, get_settings()).status()
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(payload, indent=2))
            return
        counts = payload["counts"]
        _echo(" ".join(f"{key}={value}" for key, value in counts.items()))
        run = payload.get("latest_run")
        if run:
            _echo(
                f"latest run {run['run_id']} {run['status']} "
                f"files={len(run['files'])} duration={run['duration_ms']}ms"
            )

    @exams.command("papers")
    def papers_command(
        category: str | None = typer.Option(None, "--category"),
        subject: str | None = typer.Option(None, "--subject"),
        year: int | None = typer.Option(None, "--year"),
        limit: int = typer.Option(100, "--limit", min=1),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List stored examination papers and their question/answer counts."""
        database = open_db()
        try:
            with database.session() as session:
                payload = ExamDatasetQueries(session).papers(
                    ExamFilters(
                        category_id=category,
                        subject_code=subject,
                        exam_year=year,
                    ),
                    limit=limit,
                )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(payload, indent=2))
            return
        _echo(f"{payload['total']} paper(s)")
        for paper in payload["papers"][:limit]:
            _echo(
                f"  {paper['category_id']:<14} {paper['subject_code']:<18} "
                f"{paper['exam_year'] or '-'} {paper['paper_label']:<40} "
                f"q={paper['questions']} a={paper['answers']}"
            )

    @exams.command("questions")
    def questions_command(
        category: str | None = typer.Option(None, "--category"),
        subject: str | None = typer.Option(None, "--subject"),
        course: str | None = typer.Option(None, "--course"),
        year: int | None = typer.Option(None, "--year"),
        qtype: str | None = typer.Option(None, "--type"),
        search: str | None = typer.Option(None, "--search"),
        limit: int = typer.Option(20, "--limit", min=1),
        offset: int = typer.Option(0, "--offset", min=0),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List imported examination questions with answer and source traceability."""
        database = open_db()
        try:
            with database.session() as session:
                payload = ExamDatasetQueries(session).questions(
                    ExamFilters(
                        category_id=category,
                        subject_code=subject,
                        course_id=course,
                        exam_year=year,
                        question_type=qtype,
                        search=search,
                    ),
                    limit=limit,
                    offset=offset,
                )
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(payload, indent=2))
            return
        _echo(f"{payload['total']} question(s)")
        for item in payload["questions"]:
            answer = item["answer"]
            marks = item["marks"] if item["marks"] is not None else "-"
            _echo(
                f"  {item['source_question_id']}  {item['exam_year'] or '-'} "
                f"{item['question_type']:<16} marks={marks} "
                f"answer={'yes' if answer else 'no'}"
            )
            _echo(f"    {item['text'][:160].replace(chr(10), ' ')}")

    @exams.command("syllabus")
    def syllabus_command(
        course: str = typer.Option(..., "--course"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the syllabus units and topics connected to one examination course."""
        database = open_db()
        try:
            with database.session() as session:
                payload = ExamDatasetQueries(session).syllabus(course)
        finally:
            database.close()
        if as_json:
            _echo(json.dumps(payload, indent=2))
            return
        if payload.get("error"):
            _echo(f"unknown course: {course}", err=True)
            raise typer.Exit(code=1)
        _echo(f"{payload['title']} — {payload['unit_count']} unit(s)")
        for syllabus in payload["syllabi"]:
            _echo(
                f"  syllabus {syllabus['syllabus_id'][:18]} "
                f"confidence={syllabus['confidence']:.2f} "
                f"review={'yes' if syllabus['needs_review'] else 'no'}"
            )
            for unit in syllabus["units"]:
                _echo(f"    {unit['unit_label']}: {unit['title'] or '(untitled)'}")
                for topic in unit["topics"][:6]:
                    _echo(f"      - {topic[:140]}")

    app.add_typer(exams, name="exams")


__all__ = ["register_exams_commands"]
