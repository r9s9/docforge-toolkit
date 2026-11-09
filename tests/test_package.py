"""The package itself: its entry points, its dependencies, and the doctor."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Runs the toolkit as `python -m docforge.toolkit`, with the web app's packages
# made unimportable, so a test can't pass by reaching into code the package
# doesn't ship.
BLOCKER = """
import importlib.abc, runpy, sys
BLOCKED = {"anthropic", "fastapi", "httpx", "jwt", "openai", "psycopg2",
           "pydantic_settings", "sqlalchemy", "starlette", "uvicorn"}
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked: " + name)
sys.meta_path.insert(0, Blocker())
sys.path.insert(0, sys.argv[1])
sys.argv = ["docforge-live"] + sys.argv[2:]
runpy.run_module("docforge.toolkit", run_name="__main__", alter_sys=True)
"""


def _module(*args, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", BLOCKER, str(SRC), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=180, cwd=tmp_path,
    )


def test_module_entry_point_runs_without_web_app_packages(tmp_path):
    demo = _module("demo", "--out", tmp_path / "d", tmp_path=tmp_path)
    assert demo.returncode == 0, demo.stdout + demo.stderr
    run = _module("analyze", tmp_path / "d" / "invoice_1.docx", tmp_path / "d" / "invoice_2.docx",
                  "--work", tmp_path / "w", tmp_path=tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["proposed_fields"]


def test_help_names_the_command(tmp_path):
    out = _module("--help", tmp_path=tmp_path)
    assert out.returncode == 0 and out.stdout.startswith("usage: docforge-live")


def test_console_script_points_at_main():
    from docforge.toolkit.cli import main

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'docforge-live = "docforge.toolkit.cli:main"' in pyproject
    assert callable(main)


def test_doctor_reports_packages_and_version(cli):
    code, out = cli("doctor")
    assert out["python_ok"] is True
    assert set(out["packages"]) >= {"python-docx", "docxtpl", "lxml", "pydantic"}
    assert out["toolkit"]
    assert code == (0 if out["ok"] else 1)


def test_dependencies_match_the_doctor():
    """pyproject.toml installs what deps.py checks, from the same minimums."""
    from docforge.toolkit.deps import REQUIRED

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.S | re.M).group(1)
    declared = {
        m.group(1).lower(): m.group(2)
        for m in re.finditer(r'"([A-Za-z0-9_.-]+)>=([0-9.]+)', block)
    }
    expected = {dist.lower(): minimum for dist, minimum, _pinned in REQUIRED.values()}
    assert declared == expected


def test_doctor_installs_into_a_venv_on_managed_pythons(monkeypatch):
    from docforge.toolkit import deps

    monkeypatch.setattr(deps, "missing_requirements", lambda: ["python-docx==1.2.0"])
    monkeypatch.setattr(deps, "_externally_managed", lambda: True)
    out = deps.doctor()
    assert "--system-site-packages" in out["install"]
    assert out["install"].endswith("pip install --quiet python-docx==1.2.0")
    assert out["then_run_with"].endswith(("bin/python", "python.exe"))
    monkeypatch.setattr(deps, "_externally_managed", lambda: False)
    out = deps.doctor()
    assert out["install"].endswith("-m pip install --quiet python-docx==1.2.0")
    assert "then_run_with" not in out


def test_install_commands_survive_spaces_in_paths(monkeypatch):
    from docforge.toolkit import deps

    monkeypatch.setattr(deps.sys, "executable", "/Users/A B/Application Support/python3")
    monkeypatch.setattr(deps, "missing_requirements", lambda: ["python-docx==1.2.0"])
    for managed in (True, False):
        monkeypatch.setattr(deps, "_externally_managed", lambda m=managed: m)
        first = deps.doctor()["install"].split(" && ")[0]
        assert shlex.split(first)[0] == "/Users/A B/Application Support/python3"


def test_nothing_suggests_breaking_system_packages():
    for path in [ROOT / "README.md", SRC / "docforge" / "toolkit" / "deps.py"]:
        assert "break-system-packages" not in path.read_text(encoding="utf-8"), path


def test_ci_checks_out_the_locked_upstream_commit():
    commit = json.loads((ROOT / "tools" / "upstream.lock.json").read_text(encoding="utf-8"))["commit"]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"ref: {commit}" in ci


def test_publish_runs_only_from_a_release_through_the_pypi_environment():
    """The trusted publisher on PyPI expects publish.yml and environment pypi."""
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "release:" in publish and "types: [published]" in publish
    assert "environment:" in publish and "name: pypi" in publish
    assert "id-token: write" in publish
