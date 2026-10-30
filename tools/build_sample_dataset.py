"""Build the labelled synthetic sample dataset used by Phase 1 dev and tests.

Everything this script writes is synthetic. It is generated (not scraped) so the
repository carries no third-party content and no licence ambiguity, and the
manifest says so explicitly.

Usage: python tools/build_sample_dataset.py
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "datasets" / "sample"
RETRIEVED_AT = "2026-10-20"

# --- CS201 content ----------------------------------------------------------

CS201_PAGE_1 = [
    "Demo Institute of Technology",
    "CS201 - Data Structures and Algorithms",
    "Midterm Examination - Synthetic Practice Paper",
    "Academic Year 2024-25 | Time Allowed: 90 minutes | Maximum Marks: 50",
    "SYNTHETIC PAPER: generated for development testing, not an official examination.",
    "",
    "SECTION A - Short Answer",
    "",
    "1. Define a stack and list two of its primary operations. [5 marks]",
    "",
    "2. Explain the difference between an array and a linked list with respect to",
    "   the cost of inserting an element in the middle. [5 marks]",
    "",
    "3. State the time complexity of binary search on a sorted array of n elements",
    "   and justify your answer. [4 marks]",
]

CS201_PAGE_2 = [
    "SECTION B - Problem Solving",
    "",
    "4. Given the array [3, 1, 4, 1, 5, 9, 2, 6], show the state of the array after",
    "   each pass of insertion sort. [8 marks]",
    "",
    "5. Derive the worst-case recurrence relation for merge sort and solve it.",
    "   [10 marks]",
    "",
    "6. A binary tree has 15 nodes. What is the maximum possible height of the tree?",
    "   Justify your answer. [8 marks]",
    "",
    "7. Write pseudocode to detect a cycle in a singly linked list. [10 marks]",
]

CS201_NOTES = """CS201 Lecture Notes 03 - Stacks, Queues and Complexity (SYNTHETIC)
Demo Institute of Technology | Academic Year 2024-25

This note is synthetic material written for the ingestion pipeline test suite.
It is not copied from any textbook or lecture handout.

1. Stack
A stack is a linear structure with last-in-first-out discipline.
Operations: push(x), pop(), peek(), is_empty().
All operations run in O(1) time when the stack is backed by a dynamic array.

2. Queue
A queue is first-in-first-out. A circular buffer implements it in O(1) per
operation. For a queue with capacity n, the invariant is:
    (head + size) mod n = tail
Use <= rather than < when comparing indices, because a full buffer and an
empty buffer are distinguished by the size counter.

3. Asymptotics
Growth rates: O(1) < O(log n) < O(n) < O(n log n) < O(n^2).
For n = 1000, log2(n) is approximately 10, so binary search needs at most
10 comparisons in the worst case.

4. Mathematical notation that must survive cleaning
    sum_{i=1}^{n} i = n(n + 1) / 2
    T(n) = 2T(n/2) + O(n) => T(n) = O(n log n)
    f(x) = x^2 + 3x - 5, with f'(x) = 2x + 3
These expressions must not be rewritten or stripped by the ingestion layer.
"""

CS201_UNITS = [
    (
        "Unit 1: Foundations of Algorithm Analysis (8 hours)",
        [
            "Asymptotic notation: O, Omega and Theta",
            "Recurrence relations and the master theorem",
            "Best case, average case and worst case analysis",
        ],
    ),
    (
        "Unit 2: Linear Structures (10 hours)",
        [
            "Arrays, dynamic arrays and amortised analysis",
            "Singly and doubly linked lists",
            "Stacks and queues; expression evaluation",
        ],
    ),
    (
        "Unit 3: Trees (10 hours)",
        [
            "Binary trees and traversals: preorder, inorder, postorder",
            "Binary search trees and balancing",
            "Heaps and priority queues",
        ],
    ),
    (
        "Unit 4: Graphs (12 hours)",
        [
            "Representations: adjacency matrix and adjacency list",
            "Breadth-first search and depth-first search",
            "Shortest paths: Dijkstra's algorithm",
        ],
    ),
    (
        "Unit 5: Sorting and Hashing (10 hours)",
        [
            "Comparison sorting: merge sort and quicksort",
            "Lower bounds for comparison sorting",
            "Hash tables, collisions and open addressing",
        ],
    ),
]

CS201_BANK = [
    {
        "number": "1",
        "text": "Define a stack and describe its last-in-first-out behaviour.",
        "marks": "5",
        "section": "SECTION A",
        "unit": "Unit 2",
        "difficulty": "easy",
        "answer": (
            "A stack supports push and pop at one end; the most recent element is removed first."
        ),
        "tags": "stack; LIFO; abstract data type",
    },
    {
        "number": "2",
        "text": "Compare arrays and linked lists for mid-sequence insertion.",
        "marks": "5",
        "section": "SECTION A",
        "unit": "Unit 2",
        "difficulty": "easy",
        "tags": "array; linked list",
    },
    {
        "number": "3",
        "text": "Show that binary search on a sorted array runs in O(log n).",
        "marks": "4",
        "section": "SECTION A",
        "unit": "Unit 1",
        "difficulty": "medium",
        "tags": "binary search; complexity",
    },
    {
        "number": "4",
        "text": "Trace insertion sort on the array [3, 1, 4, 1, 5, 9, 2, 6].",
        "marks": "8",
        "section": "SECTION B",
        "unit": "Unit 5",
        "difficulty": "medium",
        "tags": "insertion sort; tracing",
    },
    {
        "number": "5",
        "text": "Derive and solve the worst-case recurrence for merge sort.",
        "marks": "10",
        "section": "SECTION B",
        "unit": "Unit 5",
        "difficulty": "hard",
        "tags": "merge sort; recurrence",
    },
    {
        "number": "6",
        "text": "What is the maximum height of a binary tree with 15 nodes?",
        "marks": "8",
        "section": "SECTION B",
        "unit": "Unit 3",
        "difficulty": "medium",
        "tags": "binary tree; height",
    },
    {
        "number": "7",
        "text": "Write pseudocode that detects a cycle in a singly linked list.",
        "marks": "10",
        "section": "SECTION B",
        "unit": "Unit 2",
        "difficulty": "hard",
        "tags": "linked list; cycle detection; Floyd",
    },
    {
        "number": "8",
        "text": "Explain why comparison sorting cannot beat O(n log n) in the worst case.",
        "section": "SECTION B",
        "unit": "Unit 5",
        "difficulty": "hard",
        "tags": "lower bound; decision tree",
    },
]

# --- PHY101 content --------------------------------------------------------

PHY101_PAPER = [
    "Demo Institute of Technology",
    "PHY101 - Physics I: Mechanics and Waves",
    "Final Examination - Synthetic Practice Paper",
    "Academic Year 2024-25 | Maximum Marks: 40",
    "SYNTHETIC PAPER: generated for development testing, not an official examination.",
    "",
    "PART A - Short Answer",
    "",
    "1. State Newton's three laws of motion and give one example of each. (5 marks)",
    "",
    "2. A block of mass 2 kg slides down a frictionless incline of 30 degrees.",
    "   Calculate its acceleration. Use g = 9.8 m/s^2. (5 marks)",
    "",
    "PART B - Long Answer",
    "",
    "3. Derive the expression for the time period of a simple pendulum and state",
    "   the assumptions used. (10 marks)",
    "",
    "4. Explain the superposition principle for two travelling waves and write the",
    "   resulting expression for a standing wave. [10 marks]",
    "",
    "5. A spring of constant k = 200 N/m carries a 0.5 kg mass. Find the angular",
    "   frequency and the period of oscillation. [6]",
    "",
    "6. Describe the difference between transverse and longitudinal waves with one",
    "   example of each. (4 marks)",
]

PHY101_UNITS = [
    (
        "MODULE 1: Kinematics and Dynamics (12 hours)",
        [
            "Rectilinear motion and uniformly accelerated motion",
            "Newton's laws and inertial frames",
            "Work, energy and conservation principles",
        ],
    ),
    (
        "MODULE 2: Oscillations (8 hours)",
        [
            "Simple harmonic motion and the pendulum",
            "Damped and forced oscillations; resonance",
        ],
    ),
    (
        "MODULE 3: Waves (10 hours)",
        [
            "Transverse and longitudinal waves",
            "Superposition, interference and standing waves",
            "Sound waves and the Doppler effect",
        ],
    ),
]

PHY101_BANK = {
    "dataset_note": "SYNTHETIC - development fixture, not an official exam paper",
    "questions": [
        {
            "number": "1",
            "text": "State Newton's three laws of motion.",
            "marks": 5,
            "section": "PART A",
            "unit": "Module 1",
            "difficulty": "easy",
            "answer": "Inertia; F = ma; action-reaction pairs.",
            "tags": ["Newton", "laws of motion"],
        },
        {
            "number": "2",
            "text": (
                "Calculate the acceleration of a 2 kg block on a frictionless 30 degree incline."
            ),
            "marks": 5,
            "section": "PART A",
            "unit": "Module 1",
            "difficulty": "medium",
            "tags": ["incline", "dynamics"],
        },
        {
            "number": "3",
            "text": "Derive the time period of a simple pendulum.",
            "marks": 10,
            "section": "PART B",
            "unit": "Module 2",
            "difficulty": "hard",
            "tags": ["pendulum", "SHM"],
        },
        {
            "number": "4",
            "text": "Explain superposition for two travelling waves and derive the standing wave.",
            "marks": 10,
            "section": "PART B",
            "unit": "Module 3",
            "difficulty": "medium",
            "tags": ["waves", "superposition"],
        },
        {
            "number": "5",
            "text": "Find the angular frequency and period of a 0.5 kg mass on a 200 N/m spring.",
            "marks": 6,
            "section": "PART B",
            "unit": "Module 2",
            "difficulty": "medium",
            "tags": ["spring", "oscillation"],
        },
    ],
}


# --- writers ----------------------------------------------------------------


def write_pdf(path: Path, pages: list[list[str]], title: str) -> None:
    import fitz  # PyMuPDF

    document = fitz.open()
    document.set_metadata(
        {
            "title": title,
            "author": "Exam Engine sample builder",
            "producer": "exam-engine",
            "creator": "exam-engine",
        }
    )
    for lines in pages:
        page = document.new_page(width=595, height=842)
        y = 64.0
        for raw in lines:
            for line in textwrap.wrap(raw, width=90) or [""]:
                if y > 800:
                    page = document.new_page(width=595, height=842)
                    y = 64.0
                page.insert_text((56, y), line, fontsize=10.5, fontname="helv")
                y += 15.0
    document.save(str(path))
    document.close()


def write_docx(path: Path, course_title: str, units: list[tuple[str, list[str]]]) -> None:
    from docx import Document

    document = Document()
    document.add_heading("Syllabus - " + course_title, level=1)
    document.add_paragraph("Demo Institute of Technology")
    document.add_paragraph("Academic Year 2024-25 | Syllabus Version: v1")
    document.add_paragraph(
        "SYNTHETIC SYLLABUS: generated for development testing. It is not an official "
        "curriculum document."
    )

    document.add_heading("Assessment Scheme", level=2)
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].text = "Component"
    header[1].text = "Weight"
    header[2].text = "Notes"
    for component, weight, note in (
        ("Midterm", "30%", "Written examination"),
        ("Assignments", "20%", "Two submissions"),
        ("Final", "50%", "Written examination"),
    ):
        cells = table.add_row().cells
        cells[0].text = component
        cells[1].text = weight
        cells[2].text = note

    document.add_heading("Course Content", level=2)
    for heading, topics in units:
        document.add_paragraph(heading)
        for topic in topics:
            document.add_paragraph(topic, style="List Bullet")

    document.save(str(path))


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    import csv

    fieldnames = [
        "number",
        "text",
        "marks",
        "section",
        "unit",
        "difficulty",
        "answer",
        "tags",
        "note",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            record = {key: row.get(key, "") for key in fieldnames}
            record["note"] = "SYNTHETIC - development fixture, not an official exam paper"
            writer.writerow(record)


def build() -> None:
    (SAMPLE / "cs201").mkdir(parents=True, exist_ok=True)
    (SAMPLE / "phy101").mkdir(parents=True, exist_ok=True)

    write_pdf(
        SAMPLE / "cs201" / "question_paper_midterm.pdf",
        [CS201_PAGE_1, CS201_PAGE_2],
        "CS201 Midterm - Synthetic Practice Paper",
    )
    write_docx(
        SAMPLE / "cs201" / "syllabus.docx",
        "CS201 Data Structures and Algorithms",
        CS201_UNITS,
    )
    write_text(SAMPLE / "cs201" / "lecture_notes_stacks.txt", CS201_NOTES)
    write_csv(SAMPLE / "cs201" / "question_bank.csv", CS201_BANK)

    write_text(
        SAMPLE / "phy101" / "question_paper_final.txt",
        "\n".join(PHY101_PAPER) + "\n",
    )
    write_text(
        SAMPLE / "phy101" / "syllabus.txt",
        "\n".join(
            [
                "PHY101 Physics I: Mechanics and Waves - Syllabus (SYNTHETIC)",
                "Demo Institute of Technology | Academic Year 2024-25 | Version v1",
                "",
                *[
                    line
                    for heading, topics in PHY101_UNITS
                    for line in [heading, *[f"- {topic}" for topic in topics], ""]
                ],
            ]
        ),
    )
    write_text(
        SAMPLE / "phy101" / "question_bank.json",
        json.dumps(PHY101_BANK, indent=2) + "\n",
    )

    manifest = {
        "version": 1,
        "dataset": {
            "source_id": "demo-synthetic-2024",
            "name": "Phase 1 synthetic sample dataset",
            "dataset_type": "synthetic",
            "retrieved_at": RETRIEVED_AT,
            "license": "CC0-1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "publisher": "Exam Engine project",
            "notes": (
                "Every document here was generated by tools/build_sample_dataset.py. "
                "Questions, syllabi and notes are synthetic development fixtures and "
                "must never be presented as official exam papers."
            ),
        },
        "courses": [
            {
                "course_id": "demo-cs201-2024-v1",
                "course_code": "CS201",
                "title": "Data Structures and Algorithms",
                "institution_code": "demo-iot",
                "institution_name": "Demo Institute of Technology",
                "department": "Computer Science and Engineering",
                "programme": "B.Tech",
                "academic_year": "2024-25",
                "syllabus_version": "v1",
                "description": "Synthetic sample course for ingestion development.",
                "documents": [
                    {
                        "path": "cs201/question_paper_midterm.pdf",
                        "kind": "question_paper",
                        "title": "CS201 Midterm - Synthetic Practice Paper",
                        "academic_year": "2024-25",
                        "language": "en",
                    },
                    {
                        "path": "cs201/syllabus.docx",
                        "kind": "syllabus",
                        "title": "CS201 Syllabus (synthetic)",
                    },
                    {
                        "path": "cs201/lecture_notes_stacks.txt",
                        "kind": "notes",
                        "title": "CS201 Lecture Notes 03 (synthetic)",
                    },
                    {
                        "path": "cs201/question_bank.csv",
                        "kind": "question_bank",
                        "title": "CS201 Question Bank (synthetic)",
                    },
                ],
            },
            {
                "course_id": "demo-phy101-2024-v1",
                "course_code": "PHY101",
                "title": "Physics I: Mechanics and Waves",
                "institution_code": "demo-iot",
                "institution_name": "Demo Institute of Technology",
                "department": "Basic Sciences",
                "programme": "B.Tech",
                "academic_year": "2024-25",
                "syllabus_version": "v1",
                "description": "Second synthetic sample course for multi-course tests.",
                "documents": [
                    {
                        "path": "phy101/question_paper_final.txt",
                        "kind": "question_paper",
                        "title": "PHY101 Final - Synthetic Practice Paper",
                    },
                    {
                        "path": "phy101/syllabus.txt",
                        "kind": "syllabus",
                        "title": "PHY101 Syllabus (synthetic)",
                    },
                    {
                        "path": "phy101/question_bank.json",
                        "kind": "question_bank",
                        "title": "PHY101 Question Bank (synthetic)",
                    },
                ],
            },
        ],
    }
    (SAMPLE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote sample dataset to {SAMPLE}")


if __name__ == "__main__":
    build()
