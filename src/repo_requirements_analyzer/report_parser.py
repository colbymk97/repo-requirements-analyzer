from __future__ import annotations

import re
from dataclasses import dataclass

from .storage import EvidenceRecord, FeatureRecord, RecommendationRecord, StoryRecord


@dataclass
class ParsedReport:
    title: str
    features: list[FeatureRecord]
    stories: list[StoryRecord]
    recommendations: list[RecommendationRecord]
    evidence: list[EvidenceRecord]


SECTION_RE = re.compile(
    r"^(?:##\s+(\d+)\.\s+(.+)|\*\*(\d+)\.\s+(.+?)\*\*)\s*$",
    flags=re.MULTILINE,
)


def _section_map(markdown: str) -> dict[int, str]:
    matches = list(SECTION_RE.finditer(markdown))
    out: dict[int, str] = {}
    for idx, match in enumerate(matches):
        section_num = int(match.group(1) or match.group(3))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        out[section_num] = markdown[start:end].strip()
    return out


def _first_sentence(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return "Untitled Analysis"
    sentence = clean.split(".")[0].strip()
    return sentence[:120] if sentence else "Untitled Analysis"


def _parse_markdown_table(section_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Skip markdown table separator rows.
        if re.fullmatch(r"\|\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append(cells)
    return rows


def _parse_features(section_text: str) -> list[FeatureRecord]:
    features: list[FeatureRecord] = []
    current_domain = "General"
    for raw_line in section_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            current_domain = line[4:].strip() or "General"
            continue
        italic_domain = re.match(r"^\*(.+)\*\s*$", line.strip())
        if italic_domain:
            current_domain = italic_domain.group(1).strip() or "General"
            continue
        if line.lstrip().startswith("- "):
            text = line.lstrip()[2:].strip()
            if text:
                features.append(FeatureRecord(domain=current_domain, feature_text=text))
    return features


def _parse_stories(section_text: str) -> list[StoryRecord]:
    rows = _parse_markdown_table(section_text)
    if not rows:
        # Fallback for numbered list format:
        # 1. As a **Persona**, I want..., so that... (`file.php`)
        stories: list[StoryRecord] = []
        for raw_line in section_text.splitlines():
            line = raw_line.strip()
            m = re.match(r"^(\d+)\.\s+(As a .+)$", line, flags=re.IGNORECASE)
            if not m:
                continue
            story_num = int(m.group(1))
            story_text = m.group(2).strip()
            persona_match = re.search(r"^As a\s+\*\*(.+?)\*\*", story_text, flags=re.IGNORECASE)
            if not persona_match:
                persona_match = re.search(r"^As a[n]?\s+([^,]+),", story_text, flags=re.IGNORECASE)
            persona = persona_match.group(1).strip() if persona_match else "Unknown"
            evidence_matches = re.findall(r"`([^`]+)`", story_text)
            evidence = ", ".join(evidence_matches)
            stories.append(
                StoryRecord(
                    story_num=story_num,
                    persona=persona,
                    story_text=story_text,
                    evidence=evidence,
                )
            )
        return stories

    header = [c.lower() for c in rows[0]]
    # Expect: #, Persona, Story, Evidence
    idx_num = header.index("#") if "#" in header else 0
    idx_persona = header.index("persona") if "persona" in header else 1
    idx_story = header.index("story") if "story" in header else 2
    idx_evidence = header.index("evidence") if "evidence" in header else 3

    stories: list[StoryRecord] = []
    for row in rows[1:]:
        if len(row) <= max(idx_num, idx_persona, idx_story, idx_evidence):
            continue
        num_text = row[idx_num].strip()
        try:
            story_num = int(num_text)
        except ValueError:
            story_num = None
        stories.append(
            StoryRecord(
                story_num=story_num,
                persona=row[idx_persona].strip(),
                story_text=row[idx_story].strip(),
                evidence=row[idx_evidence].strip(),
            )
        )
    return stories


def _parse_recommendations(section_text: str) -> list[RecommendationRecord]:
    recs: list[RecommendationRecord] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if not m:
            continue
        recs.append(RecommendationRecord(item_num=int(m.group(1)), text=m.group(2).strip()))
    return recs


def _parse_evidence(section_text: str) -> list[EvidenceRecord]:
    rows = _parse_markdown_table(section_text)
    if len(rows) < 2:
        return []

    header = [c.lower() for c in rows[0]]
    idx_item = 0
    idx_source = 1 if len(header) > 1 else 0
    for i, col in enumerate(header):
        if "story" in col or "feature" in col or "item" in col:
            idx_item = i
        if "file" in col or "evidence" in col or "source" in col:
            idx_source = i

    evidence: list[EvidenceRecord] = []
    for row in rows[1:]:
        if len(row) <= max(idx_item, idx_source):
            continue
        evidence.append(EvidenceRecord(item=row[idx_item].strip(), source_paths=row[idx_source].strip()))
    return evidence


def parse_report(markdown: str) -> ParsedReport:
    sections = _section_map(markdown)
    summary = sections.get(1, "")
    title = _first_sentence(summary)
    return ParsedReport(
        title=title,
        features=_parse_features(sections.get(3, "")),
        stories=_parse_stories(sections.get(4, "")),
        recommendations=_parse_recommendations(sections.get(8, "")),
        evidence=_parse_evidence(sections.get(6, "")),
    )
