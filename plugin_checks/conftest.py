"""Checks for the DocForge.live plugin (r9s9/docforge-claude-plugin).

The plugin may hold only Markdown, JSON, YAML and its icon, so its checks live
here. Run them against a copy of the plugin:

    pytest plugin_checks --plugin ../docforge-claude-plugin
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

TOOLKIT = Path(__file__).resolve().parents[1]


def pytest_addoption(parser):
    parser.addoption(
        "--plugin",
        default=str(TOOLKIT.parent / "docforge-claude-plugin"),
        help="the plugin repository to check (default: next to this one)",
    )


@pytest.fixture(scope="session")
def plugin(request) -> Path:
    root = Path(request.config.getoption("--plugin")).resolve()
    assert (root / ".claude-plugin" / "plugin.json").is_file(), f"no plugin at {root}"
    return root


def plugin_files(root: Path) -> list[Path]:
    """The files that would be committed: what a zip of the repository holds."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True, capture_output=True, timeout=60,
        ).stdout.decode("utf-8")
        paths = [root / p for p in out.split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "results"}
        paths = [p for p in root.rglob("*") if p.is_file() and not skip & set(p.relative_to(root).parts)]
    return sorted(p for p in paths if p.exists() or p.is_symlink())
