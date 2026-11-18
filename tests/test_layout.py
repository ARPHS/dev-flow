import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def skill_name(path: Path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\nname:\s*([^\n]+)\n", text)
    assert match, f"missing frontmatter name: {path}"
    return match.group(1).strip()


def test_manifests():
    root_manifest = read_json(ROOT / "plugin.json")
    claude_manifest = read_json(ROOT / ".claude-plugin/plugin.json")
    codex_marketplace = read_json(ROOT / ".agents/plugins/marketplace.json")
    claude_marketplace = read_json(ROOT / ".claude-plugin/marketplace.json")
    assert root_manifest["name"] == "dev-flow"
    assert claude_manifest["name"] == root_manifest["name"]
    assert claude_manifest["version"] == root_manifest["version"]
    assert codex_marketplace["plugins"][0]["name"] == root_manifest["name"]
    assert claude_marketplace["plugins"][0]["name"] == root_manifest["name"]
    agent_names = set()
    for relative in claude_manifest["agents"]:
        agent = ROOT / relative
        assert re.search(r"^name:\s*df-", agent.read_text(encoding="utf-8"), re.MULTILINE)
        agent_names.add(agent.stem)
    assert agent_names == {"df-implementer", "df-reviewer"}


def test_skills():
    expected = {"df-flow", "df-plan", "df-review", "df-debug", "df-verify"}
    actual = set()
    for skill in (ROOT / "skills").iterdir():
        if skill.is_dir() and (skill / "SKILL.md").exists():
            actual.add(skill.name)
            assert skill_name(skill / "SKILL.md") == skill.name
            metadata = (skill / "agents/openai.yaml").read_text(encoding="utf-8")
            assert f'  display_name: "{skill.name}"' in metadata
            assert f"${skill.name} " in metadata
    assert actual == expected


def test_no_project_runtime_config():
    assert not (ROOT / "CLAUDE.md").exists()
    assert not (ROOT / ".claude").exists()


if __name__ == "__main__":
    test_manifests()
    test_skills()
    test_no_project_runtime_config()
    print("dev-flow layout checks passed")
