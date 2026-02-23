from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RepoScan:
    repo_path: Path
    files: list[str]
    api_endpoints: list[str]
    db_tables: list[str]
    cli_commands: list[str]
    frontend_routes: list[str]
    test_files: list[str]

    def to_dict(self) -> dict:
        return {
            "repo_path": str(self.repo_path),
            "files": self.files,
            "api_endpoints": self.api_endpoints,
            "db_tables": self.db_tables,
            "cli_commands": self.cli_commands,
            "frontend_routes": self.frontend_routes,
            "test_files": self.test_files,
        }


def _safe_read(path: Path, max_chars: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def build_scan(repo_path: Path) -> RepoScan:
    files = [
        str(p.relative_to(repo_path))
        for p in sorted(repo_path.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    ]

    api_endpoints: set[str] = set()
    db_tables: set[str] = set()
    cli_commands: set[str] = set()
    frontend_routes: set[str] = set()

    endpoint_patterns = [
        re.compile(r"@app\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]", re.I),
        re.compile(r"router\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]", re.I),
    ]
    table_pattern = re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)", re.I)
    cli_pattern = re.compile(r"add_parser\(\s*['\"]([^'\"]+)['\"]")
    route_patterns = [
        re.compile(r"<Route\s+path=['\"]([^'\"]+)['\"]", re.I),
        re.compile(r"path\s*:\s*['\"]([^'\"]+)['\"]", re.I),
    ]

    for rel in files:
        p = repo_path / rel
        lower = rel.lower()
        if not any(
            token in lower
            for token in ("api.py", "router", "routes", "sql", "schema", "cli.py", "app.tsx", "routes.ts", "pages")
        ):
            continue

        content = _safe_read(p)
        if not content:
            continue

        for pat in endpoint_patterns:
            for m in pat.finditer(content):
                api_endpoints.add(f"{m.group(1).upper()} {m.group(2)}")

        for m in table_pattern.finditer(content):
            db_tables.add(m.group(1))

        for m in cli_pattern.finditer(content):
            cli_commands.add(m.group(1))

        for pat in route_patterns:
            for m in pat.finditer(content):
                frontend_routes.add(m.group(1))

    test_files = [
        f
        for f in files
        if any(token in f.lower() for token in ("/tests/", "test_", "_test.", "spec."))
    ]

    return RepoScan(
        repo_path=repo_path,
        files=files,
        api_endpoints=sorted(api_endpoints),
        db_tables=sorted(db_tables),
        cli_commands=sorted(cli_commands),
        frontend_routes=sorted(frontend_routes),
        test_files=sorted(test_files),
    )


def write_scan(scan: RepoScan, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(scan.to_dict(), indent=2), encoding="utf-8")
