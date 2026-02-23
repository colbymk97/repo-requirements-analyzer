from __future__ import annotations

import argparse
from pathlib import Path

from .quality import ValidationResult, validate_report
from .report_parser import parse_report
from .storage import (
    connect_db,
    init_schema,
    insert_evidence,
    insert_features,
    insert_recommendations,
    insert_report,
    insert_stories,
)


def ingest_report_to_db(
    *,
    report_markdown: str,
    db_path: Path,
    repo: str | None = None,
    model: str | None = None,
    report_path: Path | None = None,
    validation_result: ValidationResult | None = None,
    min_stories: int = 15,
    min_evidence: int = 25,
) -> int:
    parsed = parse_report(report_markdown)
    computed_validation = validation_result or validate_report(
        report_markdown,
        min_stories=min_stories,
        min_evidence=min_evidence,
    )
    conn = connect_db(db_path)
    init_schema(conn)
    report_id = insert_report(
        conn,
        title=parsed.title,
        repo=repo,
        model=model,
        report_path=str(report_path.resolve()) if report_path else None,
        markdown=report_markdown,
        validation_status="passed" if computed_validation.passed else "warning",
        validation_errors="\n".join(computed_validation.errors),
        validation_error_count=len(computed_validation.errors),
    )
    insert_features(conn, report_id, parsed.features)
    insert_stories(conn, report_id, parsed.stories)
    insert_recommendations(conn, report_id, parsed.recommendations)
    insert_evidence(conn, report_id, parsed.evidence)
    conn.close()
    return report_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest generated markdown analysis reports into SQLite.")
    parser.add_argument("--report", required=True, help="Path to markdown report file.")
    parser.add_argument("--db", default="./data/specs.db", help="SQLite DB path (default: ./data/specs.db).")
    parser.add_argument("--repo", default="", help="Optional repo URL/path metadata.")
    parser.add_argument("--model", default="", help="Optional model metadata.")
    parser.add_argument("--min-stories", type=int, default=15, help="Minimum stories for quality validation.")
    parser.add_argument("--min-evidence", type=int, default=25, help="Minimum evidence rows/paths for validation.")
    return parser.parse_args()


def entrypoint() -> None:
    args = parse_args()
    report_path = Path(args.report).expanduser().resolve()
    markdown = report_path.read_text(encoding="utf-8")
    report_id = ingest_report_to_db(
        report_markdown=markdown,
        db_path=Path(args.db),
        repo=args.repo or None,
        model=args.model or None,
        report_path=report_path,
        min_stories=args.min_stories,
        min_evidence=args.min_evidence,
    )
    print(f"Ingested report_id={report_id} into {Path(args.db).expanduser().resolve()}")


if __name__ == "__main__":
    entrypoint()
