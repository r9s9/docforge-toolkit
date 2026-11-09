"""The eval suite stays loadable: every case and grader matches the documented format.

Format reference: Claude Code's plugin evals docs (prompt.md frontmatter,
grader frontmatter and grader types). An unknown key fails a case at run
time, so it should fail here first.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPT_KEYS = {
    "schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome",
    "model", "max_turns", "timeout_seconds", "allowed_tools", "append_system_prompt", "env",
}
GRADER_OPTIONS = {
    "regex": ({"pattern"}, {"flags", "match", "target"}),
    "tool_used": ({"tool"}, {"input_match", "min", "max"}),
    "tool_order": ({"before", "after"}, set()),
    "file_exists": ({"path"}, {"exists"}),
    "llm": (set(), {"criteria", "focus"}),
    "baseline": ({"baseline_file"}, {"criteria"}),
}
COMMON = {"type", "weight", "arm"}
WORKFLOWS = ("make-template", "fill-template", "check-document")


def _split(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    assert m, f"{path} needs YAML frontmatter"
    keys = {}
    for line in m.group(1).splitlines():
        top = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if top:
            keys[top.group(1)] = top.group(2).strip()
    return keys, m.group(2).strip()


def _cases(plugin: Path) -> list[Path]:
    cases = sorted(p.parent for p in (plugin / "evals").glob("*/prompt.md"))
    assert cases
    return cases


def test_there_are_cases_for_every_workflow(plugin):
    tags = " ".join(_split(c / "prompt.md")[0].get("tags", "") for c in _cases(plugin))
    for workflow in WORKFLOWS:
        assert workflow in tags, f"no eval case is tagged {workflow}"


def test_cases_are_well_formed(plugin):
    skills = {p.parent.name for p in (plugin / "skills").glob("*/SKILL.md")}
    for case in _cases(plugin):
        keys, body = _split(case / "prompt.md")
        assert set(keys) <= PROMPT_KEYS, f"{case.name}: unknown prompt.md keys {set(keys) - PROMPT_KEYS}"
        assert body, f"{case.name}: the prompt body is empty"
        assert 1 <= int(keys.get("runs", "3")) <= 50
        assert int(keys.get("max_turns", "10")) <= 200
        assert int(keys.get("timeout_seconds", "300")) <= 3600
        graders = sorted((case / "graders").glob("*.md"))
        assert graders, f"{case.name}: a case needs at least one grader"
        for grader in graders:
            g, g_body = _split(grader)
            kind = g.get("type")
            assert kind in GRADER_OPTIONS, f"{grader}: unknown type {kind!r}"
            required, optional = GRADER_OPTIONS[kind]
            assert required <= set(g), f"{grader}: needs {required - set(g)}"
            assert set(g) <= COMMON | required | optional, f"{grader}: unknown keys"
            if kind == "llm":
                assert g_body or "criteria" in g, f"{grader}: an llm grader needs a rubric"
            for skill in re.findall(r"\(\?:\[\\w-\]\+:\)\?([a-z-]+)", g.get("input_match", "")):
                assert skill in skills, f"{grader}: no skill named {skill}"
