from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class StoryRecord:
    story_num: int | None
    persona: str
    story_text: str
    evidence: str


@dataclass
class FeatureRecord:
    domain: str
    feature_text: str


@dataclass
class RecommendationRecord:
    item_num: int | None
    text: str


@dataclass
class EvidenceRecord:
    item: str
    source_paths: str


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            repo TEXT,
            model TEXT,
            report_path TEXT,
            markdown TEXT NOT NULL,
            created_at TEXT NOT NULL,
            validation_status TEXT NOT NULL DEFAULT 'unknown',
            validation_errors TEXT NOT NULL DEFAULT '',
            validation_error_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            domain TEXT NOT NULL,
            feature_text TEXT NOT NULL,
            FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            story_num INTEGER,
            persona TEXT NOT NULL,
            story_text TEXT NOT NULL,
            evidence TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            notes TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            item_num INTEGER,
            recommendation_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            notes TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            source_paths TEXT NOT NULL,
            FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
        );
        """
    )
    _ensure_report_columns(conn)
    conn.commit()


def _ensure_report_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
    if "validation_status" not in existing:
        conn.execute("ALTER TABLE reports ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'unknown'")
    if "validation_errors" not in existing:
        conn.execute("ALTER TABLE reports ADD COLUMN validation_errors TEXT NOT NULL DEFAULT ''")
    if "validation_error_count" not in existing:
        conn.execute("ALTER TABLE reports ADD COLUMN validation_error_count INTEGER NOT NULL DEFAULT 0")


def insert_report(
    conn: sqlite3.Connection,
    *,
    title: str,
    repo: str | None,
    model: str | None,
    report_path: str | None,
    markdown: str,
    validation_status: str = "unknown",
    validation_errors: str = "",
    validation_error_count: int = 0,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO reports (
            title, repo, model, report_path, markdown, created_at,
            validation_status, validation_errors, validation_error_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            repo,
            model,
            report_path,
            markdown,
            created_at,
            validation_status,
            validation_errors,
            validation_error_count,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_features(conn: sqlite3.Connection, report_id: int, features: list[FeatureRecord]) -> None:
    if not features:
        return
    conn.executemany(
        "INSERT INTO features (report_id, domain, feature_text) VALUES (?, ?, ?)",
        [(report_id, f.domain, f.feature_text) for f in features],
    )
    conn.commit()


def insert_stories(conn: sqlite3.Connection, report_id: int, stories: list[StoryRecord]) -> None:
    if not stories:
        return
    conn.executemany(
        "INSERT INTO stories (report_id, story_num, persona, story_text, evidence) VALUES (?, ?, ?, ?, ?)",
        [(report_id, s.story_num, s.persona, s.story_text, s.evidence) for s in stories],
    )
    conn.commit()


def insert_recommendations(conn: sqlite3.Connection, report_id: int, recs: list[RecommendationRecord]) -> None:
    if not recs:
        return
    conn.executemany(
        "INSERT INTO recommendations (report_id, item_num, recommendation_text) VALUES (?, ?, ?)",
        [(report_id, r.item_num, r.text) for r in recs],
    )
    conn.commit()


def insert_evidence(conn: sqlite3.Connection, report_id: int, evidence: list[EvidenceRecord]) -> None:
    if not evidence:
        return
    conn.executemany(
        "INSERT INTO evidence (report_id, item, source_paths) VALUES (?, ?, ?)",
        [(report_id, e.item, e.source_paths) for e in evidence],
    )
    conn.commit()
