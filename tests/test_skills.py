import tempfile
import unittest
from pathlib import Path

from pipeline_agent.skills import discover_skills, find_skill, parse_frontmatter, render_skill


SKILL_TEXT = """---
name: demo-skill
description: "A demo skill"
version: 1.0.0
tags:
  - one
  - two
---
# Demo

Do the demo thing.
"""


class SkillTests(unittest.TestCase):
    def test_frontmatter(self):
        metadata, body = parse_frontmatter(SKILL_TEXT)
        self.assertEqual(metadata["name"], "demo-skill")
        self.assertEqual(metadata["description"], "A demo skill")
        self.assertEqual(metadata["version"], "1.0.0")
        self.assertNotIn("tags", metadata)
        self.assertTrue(body.strip().startswith("# Demo"))

    def test_no_frontmatter(self):
        metadata, body = parse_frontmatter("just text")
        self.assertEqual(metadata, {})
        self.assertEqual(body, "just text")

    def test_discover_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo").mkdir()
            (root / "demo" / "skill.md").write_text(SKILL_TEXT, encoding="utf-8")
            (root / "demo" / "scripts").mkdir()
            (root / "demo" / "scripts" / "run.sh").write_text("echo", encoding="utf-8")
            (root / "other").mkdir()
            (root / "other" / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
            skills = discover_skills([root])
            self.assertEqual(sorted(skills), ["demo-skill", "other"])
            skill = find_skill([root], "Demo-Skill")
            self.assertIsNotNone(skill)
            rendered = render_skill(skill)
            self.assertIn("# Active skill: demo-skill", rendered)
            self.assertIn("scripts/run.sh", rendered)
            self.assertIn("Do the demo thing.", rendered)
            self.assertIsNone(find_skill([root], "none"))
            self.assertIsNone(find_skill([root], "missing"))

    def test_bundled_skills_are_valid(self):
        bundled = Path(__file__).resolve().parent.parent / "skills"
        skills = discover_skills([bundled])
        self.assertIn("fix-foss", skills)
        self.assertIn("pipeline-smoke", skills)
        for skill in skills.values():
            self.assertTrue(skill.description, skill.name)
            self.assertTrue(skill.body, skill.name)


if __name__ == "__main__":
    unittest.main()
