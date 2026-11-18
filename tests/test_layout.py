import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = {"df-flow", "df-plan", "df-review", "df-debug", "df-verify"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter(path: Path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert match, f"missing frontmatter: {path}"
    return dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)


class LayoutTest(unittest.TestCase):
    def test_manifests(self):
        root_manifest = read_json(ROOT / "plugin.json")
        claude_manifest = read_json(ROOT / ".claude-plugin/plugin.json")
        self.assertEqual(root_manifest["name"], "dev-flow")
        self.assertEqual(claude_manifest["name"], root_manifest["name"])
        self.assertEqual(claude_manifest["version"], root_manifest["version"])
        self.assertEqual(read_json(ROOT / ".agents/plugins/marketplace.json")["plugins"][0]["name"], "dev-flow")
        self.assertEqual(read_json(ROOT / ".claude-plugin/marketplace.json")["plugins"][0]["name"], "dev-flow")
        names = set()
        for relative in claude_manifest["agents"]:
            meta = frontmatter(ROOT / relative)
            self.assertTrue(meta["name"].strip().startswith("df-"))
            names.add(meta["name"].strip())
        self.assertEqual(names, {"df-implementer", "df-reviewer"})

    def test_skills(self):
        actual = set()
        for skill in (ROOT / "skills").iterdir():
            if not (skill / "SKILL.md").exists():
                continue
            actual.add(skill.name)
            meta = frontmatter(skill / "SKILL.md")
            self.assertEqual(meta["name"].strip(), skill.name)
            self.assertTrue(meta.get("description", "").strip(), skill.name)
            metadata = (skill / "agents/openai.yaml").read_text(encoding="utf-8")
            self.assertIn(f'  display_name: "{skill.name}"', metadata)
            self.assertIn(f"${skill.name} ", metadata)
        self.assertEqual(actual, SKILLS)

    def test_relative_links_resolve(self):
        for md in list((ROOT / "skills").rglob("*.md")) + list((ROOT / "agents").glob("*.md")):
            for target in re.findall(r"\]\(([^)#:]+\.md)(?:#[^)]*)?\)", md.read_text(encoding="utf-8")):
                self.assertTrue((md.parent / target).resolve().exists(), f"{md}: broken link {target}")

    def test_hard_rules_present(self):
        flow = (ROOT / "skills/df-flow/SKILL.md").read_text(encoding="utf-8")
        for marker in ("【分级】", "【基线】", "project 必须停顿", "df-verify", "df-reviewer"):
            self.assertIn(marker, flow)

    def test_no_project_runtime_config(self):
        self.assertFalse((ROOT / "CLAUDE.md").exists())
        self.assertFalse((ROOT / ".claude").exists())
        self.assertFalse((ROOT / "hooks").exists(), "hooks must stay opt-in, not auto-installed")


if __name__ == "__main__":
    unittest.main()
