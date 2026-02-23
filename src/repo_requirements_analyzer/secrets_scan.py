from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SecretFinding:
    rule_id: str
    path: str
    line: int
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "path": self.path,
            "line": self.line,
            "snippet": self.snippet,
        }


@dataclass
class SecretScan:
    repo_path: Path
    findings: list[SecretFinding]
    excluded_paths: list[str]
    stack_hints: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_path": str(self.repo_path),
            "finding_count": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
            "excluded_paths": self.excluded_paths,
            "stack_hints": self.stack_hints,
        }


_IGNORE_SUBSTRINGS = (
    "/.git/",
    "/node_modules/",
    "/dist/",
    "/build/",
    "/vendor/",
    "/.next/",
    "/coverage/",
    "/.venv/",
    "/venv/",
)

_TEXT_SUFFIX_ALLOWLIST = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".php",
    ".rb",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".cs",
    ".swift",
    ".scala",
    ".sql",
    ".ini",
    ".cfg",
    ".conf",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".env",
    ".txt",
    ".md",
    ".properties",
    ".xml",
}

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "openai_api_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "github_pat",
        re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    ),
    (
        "aws_access_key_id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    ),
    (
        "hardcoded_secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"][^'\"]{6,}['\"]"
        ),
    ),
    (
        "db_uri_with_password",
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@"),
    ),
]

_FALSE_POSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(example|dummy|sample|test|placeholder|changeme|your_)[a-z0-9_ -]*"),
    re.compile(r"(?i)\$\{?[A-Z][A-Z0-9_]+\}?"),
]


def _looks_binary(raw: bytes) -> bool:
    return b"\x00" in raw


def _redact_snippet(text: str, max_len: int = 180) -> str:
    redacted = re.sub(r"[A-Za-z0-9]{16,}", "***", text.strip())
    if len(redacted) <= max_len:
        return redacted
    return redacted[: max_len - 3] + "..."


def _is_candidate_text_file(path: Path) -> bool:
    if path.name.startswith(".env"):
        return True
    suffix = path.suffix.lower()
    return suffix in _TEXT_SUFFIX_ALLOWLIST


def _should_skip(rel_path: str) -> bool:
    normalized = "/" + rel_path.replace("\\", "/")
    for part in _IGNORE_SUBSTRINGS:
        if part in normalized:
            return True
    return False


def detect_stack_hints(repo_path: Path) -> list[str]:
    hints: set[str] = set()
    if (repo_path / "package.json").exists():
        hints.add("node")
    if (repo_path / "composer.json").exists():
        hints.add("php")
    if (repo_path / "requirements.txt").exists() or (repo_path / "pyproject.toml").exists():
        hints.add("python")
    if (repo_path / "Gemfile").exists():
        hints.add("ruby")
    if (repo_path / "pom.xml").exists() or (repo_path / "build.gradle").exists():
        hints.add("jvm")
    if (repo_path / "go.mod").exists():
        hints.add("go")
    if (repo_path / "Cargo.toml").exists():
        hints.add("rust")
    return sorted(hints)


def build_secret_scan(repo_path: Path) -> SecretScan:
    findings: list[SecretFinding] = []
    excluded: list[str] = []

    for path in sorted(repo_path.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo_path))
        if _should_skip(rel):
            excluded.append(rel)
            continue
        if not _is_candidate_text_file(path):
            continue

        raw = path.read_bytes()
        if _looks_binary(raw):
            continue
        content = raw.decode("utf-8", errors="ignore")
        if not content:
            continue

        for idx, line in enumerate(content.splitlines(), start=1):
            for rule_id, pattern in _RULES:
                if not pattern.search(line):
                    continue
                if any(fp.search(line) for fp in _FALSE_POSITIVE_PATTERNS):
                    continue
                findings.append(
                    SecretFinding(
                        rule_id=rule_id,
                        path=rel,
                        line=idx,
                        snippet=_redact_snippet(line),
                    )
                )

    return SecretScan(
        repo_path=repo_path,
        findings=findings,
        excluded_paths=sorted(set(excluded)),
        stack_hints=detect_stack_hints(repo_path),
    )


def write_secret_scan(scan: SecretScan, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(scan.to_dict(), indent=2) + "\n", encoding="utf-8")

