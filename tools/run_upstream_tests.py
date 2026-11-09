"""Run DocForge's own engine tests against the plugin's copy of the engine.

The plugin's engine is copied from upstream (see tools/sync_upstream.py), so
upstream's tests for those modules should pass unchanged against the copy.
Only test files that import nothing but engine modules are run; the rest test
the web app (models, database, services) and don't apply.

Web-app packages (sqlalchemy, fastapi, httpx, anthropic, pydantic_settings…)
are blocked while the tests run, so a test can't pass by reaching into code
the plugin doesn't ship.

Usage:
  python tools/run_upstream_tests.py --from ../docforge [-- extra pytest args]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src"

# Upstream unit tests that exercise only the copied engine modules.
CURATED = (
    "test_classify.py",
    "test_contents_follow_headings.py",
    "test_diff.py",
    "test_extraction.py",
    "test_layout_tables.py",
    "test_rich_render.py",
    "test_router_and_builder.py",
    "test_short_tags.py",
    "test_single_document_smart.py",
    "test_source_outline.py",
    "test_template_keeps_its_layout.py",
    "test_textutil.py",
    "test_validator.py",
)

# Upstream's conftest needs the database; these are the fixtures the curated
# tests use, built the same way (from docforge.sampledata).
CONFTEST = '''
"""Fixtures for upstream engine tests run against the plugin's engine copy."""

import importlib.abc
import sys

sys.path.insert(0, ENGINE)

import pytest

_BLOCKED = {
    "anthropic", "fastapi", "httpx", "jwt", "openai", "psycopg2",
    "pydantic_settings", "sqlalchemy", "starlette", "uvicorn",
}


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ImportError(f"{name} belongs to the web app, not the plugin engine")
        return None


sys.meta_path.insert(0, _Blocker())

from docforge.sampledata import write_all  # noqa: E402


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    write_all(d)
    return d


@pytest.fixture
def project_docs(sample_dir):
    return [sample_dir / "project_report_1.docx", sample_dir / "project_report_2.docx"]


@pytest.fixture
def invoice_docs(sample_dir):
    return [sample_dir / "invoice_1.docx", sample_dir / "invoice_2.docx"]


@pytest.fixture
def compliance_docs(sample_dir):
    return [sample_dir / "compliance_report_1.docx", sample_dir / "compliance_report_2.docx"]
'''


def _upstream_tests(path: Path) -> Path:
    for candidate in (path / "backend" / "tests" / "unit", path / "tests" / "unit", path):
        if (candidate / CURATED[0]).is_file():
            return candidate
    raise SystemExit(f"error: no DocForge unit tests found under {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", type=Path, required=True,
                        help="DocForge checkout at the commit in tools/UPSTREAM.md")
    parser.add_argument("pytest_args", nargs="*", help="extra arguments for pytest")
    args = parser.parse_args(argv)

    tests = _upstream_tests(args.source.resolve())
    work = Path(tempfile.mkdtemp(prefix="docforge-upstream-tests-"))
    try:
        dest = work / "upstream_tests"
        dest.mkdir()
        missing = [name for name in CURATED if not (tests / name).is_file()]
        if missing:
            print(f"warning: not found upstream (renamed or removed?): {', '.join(missing)}")
        for name in CURATED:
            if (tests / name).is_file():
                shutil.copyfile(tests / name, dest / name)
        conftest = CONFTEST.replace("sys.path.insert(0, ENGINE)", f"sys.path.insert(0, {str(ENGINE)!r})")
        (dest / "conftest.py").write_text(conftest, encoding="utf-8")
        # -B: no bytecode files in the plugin's engine folder.
        cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--rootdir", str(work), str(dest), *args.pytest_args]
        return subprocess.call(cmd, cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
