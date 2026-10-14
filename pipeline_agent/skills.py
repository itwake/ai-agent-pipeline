"""Skill discovery: a skill is a directory holding ``skill.md`` with frontmatter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

SKILL_FILE_NAMES = ("skill.md", "SKILL.md")
MAX_LISTED_FILES = 60


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    root: Path
    metadata: dict[str, str] = field(default_factory=dict)

    def files(self, limit: int = MAX_LISTED_FILES) -> list[str]:
        listed: list[str] = []
        for candidate in sorted(self.root.rglob("*")):
            if not candidate.is_file() or candidate == self.path:
                continue
            relative = candidate.relative_to(self.root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            listed.append(str(relative).replace("\\", "/"))
            if len(listed) >= limit:
                break
        return listed


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    metadata: dict[str, str] = {}
    for index in range(1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            return metadata, "\n".join(lines[index + 1 :])
        if not stripped or stripped.startswith("#") or lines[index][:1].isspace() or stripped.startswith("-"):
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            metadata[key] = value
    return {}, text


def load_skill_file(path: Path) -> Optional[Skill]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    metadata, body = parse_frontmatter(text)
    name = metadata.get("name", "").strip() or path.parent.name
    return Skill(
        name=name,
        description=metadata.get("description", "").strip(),
        body=body.strip("\n"),
        path=path,
        root=path.parent,
        metadata=metadata,
    )


def discover_skills(directories: Iterable[str | Path]) -> dict[str, Skill]:
    """Map lower-cased skill name to Skill. Earlier directories win on conflicts."""

    found: dict[str, Skill] = {}
    for directory in directories:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        candidates: list[Path] = []
        for file_name in SKILL_FILE_NAMES:
            direct = root / file_name
            if direct.is_file():
                candidates.append(direct)
            candidates.extend(sorted(p for p in root.glob(f"*/{file_name}") if p.is_file()))
        for skill_file in candidates:
            skill = load_skill_file(skill_file)
            if skill is None:
                continue
            key = skill.name.lower()
            if key not in found:
                found[key] = skill
    return found


def find_skill(directories: Iterable[str | Path], name: str) -> Optional[Skill]:
    key = (name or "").strip().lower()
    if not key or key == "none":
        return None
    return discover_skills(directories).get(key)


def render_skill(skill: Skill) -> str:
    lines = [f"# Active skill: {skill.name}"]
    if skill.description:
        lines.append(skill.description)
    lines.append(f"Skill directory (read-only reference material; run its scripts with bash): {skill.root.resolve()}")
    files = skill.files()
    if files:
        lines.append("Files in the skill directory: " + ", ".join(files))
    lines.append("")
    lines.append(skill.body)
    return "\n".join(lines)


__all__ = ["Skill", "discover_skills", "find_skill", "load_skill_file", "parse_frontmatter", "render_skill"]
