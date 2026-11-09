"""Shared fixtures: the engine on sys.path, sample documents, and a CLI runner."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # the source tree, installed or not

from docforge.toolkit.cli import main  # noqa: E402


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory) -> Path:
    """The demo documents: six samples and an edited invoice."""
    out = tmp_path_factory.mktemp("demo")
    code, result = run_cli("demo", "--out", str(out))
    assert code == 0, result
    return out


def run_cli(*args: str) -> tuple[int, dict]:
    """Run a CLI command in-process; return (exit code, parsed JSON output)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main([str(a) for a in args])
    return code, json.loads(buf.getvalue())


@pytest.fixture
def cli():
    return run_cli


@pytest.fixture
def invoice_package(tmp_path, demo_dir, cli) -> Path:
    """An invoice template built from the two demo invoices, with light edits."""
    work = tmp_path / "work"
    code, out = cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx",
                    "--work", work, "--mode", "smart")
    assert code == 0, out
    (work / "decisions.json").write_text(json.dumps({
        "document_type": "Commercial invoice",
        "classifications": [
            {"node_id": "n0004", "field_name": "customer",
             "description": "The company or person being billed."},
        ],
        "fields": {"line_items": {"columns": {"qty": {"label": "Quantity"}}}},
    }))
    package = tmp_path / "templates" / "invoice"
    code, out = cli("build", "--work", work, "--name", "Invoice", "--out", package)
    assert code == 0, out
    return package
