"""The skill and the commands: spec, routing, the toolkit they call, its pinned version."""

from __future__ import annotations

import re
from pathlib import Path

SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
COMMAND_KEYS = {"description", "argument-hint"}
WORKFLOWS = ("make-template.md", "fill-template.md", "check-document.md")
PIN = re.compile(r"docforge-live==([0-9][0-9A-Za-z.]*)")


def _skills(plugin: Path) -> list[Path]:
    skills = sorted(p.parent for p in (plugin / "skills").glob("*/SKILL.md"))
    assert skills
    return skills


def _front(text: str) -> tuple[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, "needs YAML frontmatter"
    return m.group(1), m.group(2)


def test_skill_follows_the_agent_skills_spec(plugin):
    """The rules skills-ref checks (agentskills.io/specification), plus the
    claude.ai upload's "invalid characters in name or description"."""
    for skill in _skills(plugin):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        front, body = _front(text)
        keys = set(re.findall(r"^([A-Za-z_-]+):", front, re.M))
        assert keys <= SPEC_KEYS, f"frontmatter keys outside the spec: {keys - SPEC_KEYS}"
        name = re.search(r"^name: (.+)$", front, re.M).group(1).strip()
        description = re.search(r"^description: (.+)$", front, re.M).group(1).strip()
        assert name == skill.name
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) and len(name) <= 64
        assert 0 < len(description) <= 1024
        assert not set(description) & set("<>"), "no XML-like tags in the description"
        assert description.isascii()
        assert len(text.splitlines()) < 500
        assert len(body.split()) < 3500, "keep SKILL.md under ~5000 tokens"
        # Reference files sit one level deep: SKILL.md names every one of them.
        linked = set(re.findall(r"`(references/[\w.-]+)`", text))
        present = {f"references/{p.name}" for p in (skill / "references").glob("*.md")}
        assert present <= linked, f"not linked from SKILL.md: {present - linked}"
        for ref in linked:
            assert (skill / ref).is_file(), f"{ref} is linked but missing"


def test_docs_mention_only_real_toolkit_commands_and_all_of_them(plugin):
    from docforge.toolkit.cli import build_parser

    commands = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    used: set[str] = set()
    for skill in _skills(plugin):
        for doc in [skill / "SKILL.md", *sorted((skill / "references").glob("*.md"))]:
            used |= set(re.findall(r"\bdocforge ([a-z][a-z-]+)", doc.read_text(encoding="utf-8")))
    assert used <= commands, f"unknown commands: {used - commands}"
    assert commands <= used, f"undocumented commands: {commands - used}"


def test_one_pinned_toolkit_version_everywhere(plugin):
    """Every docforge-live==X.Y.Z, the version SKILL.md names and the one the
    doctor must report all agree, so the skill never mixes two toolkits."""
    pins = set()
    for path in plugin.rglob("*.md"):
        if ".git" not in path.parts:
            pins |= set(PIN.findall(path.read_text(encoding="utf-8")))
    assert len(pins) == 1, f"pins disagree: {pins}"
    (pin,) = pins
    for skill in _skills(plugin):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert f"This skill uses version {pin}." in " ".join(text.split())
        assert f'"toolkit": "{pin}"' in text
    readme = " ".join((plugin / "README.md").read_text(encoding="utf-8").split())
    assert f"`docforge-live` version {pin}" in readme


def test_skill_installs_only_the_toolkit_and_never_over_the_system(plugin):
    for path in [*plugin.glob("skills/*/SKILL.md"), *plugin.glob("skills/*/references/*.md"),
                 plugin / "README.md"]:
        text = path.read_text(encoding="utf-8")
        assert "break-system-packages" not in text, path
        for line in re.findall(r"pip install[^\n`]*", text):
            assert "docforge-live==" in line, f"{path.name}: installs something else: {line}"


def test_there_is_a_command_per_workflow(plugin):
    assert sorted(p.name for p in (plugin / "commands").glob("*.md")) == sorted(WORKFLOWS)


def test_commands_route_to_the_docforge_skill(plugin):
    """Commands load in Code and Cowork as /docforge-live:<name>, and in chat as a
    skill. They carry no files of their own, so each hands over to the docforge
    skill's workflow."""
    skill = plugin / "skills" / "docforge"
    for command in sorted((plugin / "commands").glob("*.md")):
        front, body = _front(command.read_text(encoding="utf-8"))
        keys = set(re.findall(r"^([A-Za-z_-]+):", front, re.M))
        assert keys <= COMMAND_KEYS and "description" in keys, keys
        description = re.search(r"^description: (.+)$", front, re.M).group(1).strip()
        assert 0 < len(description) <= 250 and description.isascii()
        assert not set(description) & set("<>")
        assert "DocForge" in description, "keep the description from matching unrelated Word tasks"
        workflow = f"references/{command.stem}.md"
        assert f"`{workflow}`" in body and (skill / workflow).is_file()
        title = (skill / workflow).read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
        assert f'"{title}"' in body, f"name the workflow as its file does: {title}"
        assert "**docforge** skill" in body and "`SKILL.md`" in body
        # Cowork and Code substitute every $ARGUMENTS, so it appears once.
        assert body.count("$ARGUMENTS") == 1
        assert "${" not in body, "chat may leave placeholders unfilled; commands need no paths"


def test_every_workflow_hands_over_its_files(plugin):
    skill = plugin / "skills" / "docforge"
    assert "## Hand over the files" in (skill / "SKILL.md").read_text(encoding="utf-8")
    for workflow in WORKFLOWS:
        assert '"Hand over the files"' in (skill / "references" / workflow).read_text(encoding="utf-8")
    assert "hand_over" in (skill / "references" / "make-template.md").read_text(encoding="utf-8")
